"""
PolicyRuntimeRegistry — registro local dos conjuntos ativos de policy.

Responsabilidade:
    Manter em memória o ActivePolicySet por policy_scope_id, gerenciar
    o ciclo de vida de refresh, garantir swap atômico e implementar
    a política de fallback para o Last Known Good (LKG).

Modelo operacional (Requisito 6.1 – 6.5):
    Em steady state, get_active_policy_set() retorna o ActivePolicySet
    já carregado em memória sem nenhum I/O. O hot path é uma leitura
    de dicionário.

    O refresh ocorre fora do hot path:
    1. ManifestResolver lê o manifesto ativo do AppConfig.
    2. Se o activation_id mudou, BundleLoader e SnapshotLoader carregam
       os artefatos do S3.
    3. Integridade e compatibilidade são verificadas.
    4. O ActivePolicySet é trocado por swap atômico (substituição de
       referência no dicionário — atômica em CPython pelo GIL).
    5. O novo conjunto é salvo no LKGStore como Last Known Good.

Política de degradação (Requisito 17.1, 17.2):
    - Cold start sem policy válida: levanta PolicyEngineNotReady (503).
    - Após boot válido, falha de refresh: usa Last Known Good.
    - Bundle/snapshot com integridade inválida: rejeita e alarma.

Thread safety:
    O swap atômico de referência de dicionário é seguro em CPython
    (GIL garante atomicidade de atribuições de referência). Para
    ambientes multi-thread com múltiplos refreshes concorrentes,
    considere adicionar um threading.Lock ao redor do bloco de refresh.

Requisitos cobertos: 6.1, 6.2, 6.3, 6.4, 6.5, 17.1, 17.2, 17.3, 17.4
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from validation_engine.domain.errors import (
    PolicyBundleIntegrityFailure,
    PolicyBundleUnavailable,
    PolicyEngineNotReady,
    PolicySnapshotUnavailable,
    ValidationEngineError,
)
from validation_engine.domain.models import ActivePolicySet

if TYPE_CHECKING:
    from validation_engine.infrastructure.bundle_loader import BundleLoader
    from validation_engine.infrastructure.lkg_store import LKGStore
    from validation_engine.infrastructure.manifest_resolver import ManifestResolver
    from validation_engine.infrastructure.snapshot_loader import SnapshotLoader

logger = logging.getLogger(__name__)


class PolicyRuntimeRegistry:
    """
    Registro local dos conjuntos ativos de policy por escopo.

    Mantém um cache em memória de ActivePolicySets indexados por
    policy_scope_id. Em steady state, o hot path usa apenas leitura
    de memória sem I/O.

    O refresh é disparado explicitamente via refresh_scope() ou
    implicitamente via get_active_policy_set() quando o escopo
    ainda não foi inicializado.

    Uso típico:
        registry = PolicyRuntimeRegistry(
            manifest_resolver=resolver,
            bundle_loader=bundle_loader,
            snapshot_loader=snapshot_loader,
            lkg_store=lkg_store,
            evaluator_version="1.0.0",
        )

        # Bootstrap inicial (deve ser chamado na inicialização do serviço):
        registry.refresh_scope("tenantA:TRANSFER:PIX:*:prod")

        # Hot path (sem I/O em steady state):
        active_set = registry.get_active_policy_set("tenantA:TRANSFER:PIX:*:prod")
    """

    def __init__(
        self,
        manifest_resolver: "ManifestResolver",
        bundle_loader: "BundleLoader",
        snapshot_loader: "SnapshotLoader",
        lkg_store: "LKGStore",
        evaluator_version: str,
    ) -> None:
        """
        Inicializa o PolicyRuntimeRegistry.

        Args:
            manifest_resolver: resolve PolicyActivationManifests do AppConfig.
            bundle_loader:     carrega RuleBundles do S3 com cache e verificação.
            snapshot_loader:   carrega ReferenceSnapshots do S3 com cache.
            lkg_store:         persiste e carrega o Last Known Good em disco.
            evaluator_version: versão do evaluator em execução (para logs e auditoria).
        """
        self._manifest_resolver = manifest_resolver
        self._bundle_loader = bundle_loader
        self._snapshot_loader = snapshot_loader
        self._lkg_store = lkg_store
        self._evaluator_version = evaluator_version

        # Cache em memória: policy_scope_id → ActivePolicySet.
        # Em steady state, get_active_policy_set() lê apenas deste dicionário.
        self._active_sets: dict[str, ActivePolicySet] = {}

        logger.info(
            "PolicyRuntimeRegistry inicializado",
            extra={"evaluator_version": evaluator_version},
        )

    def get_active_policy_set(self, policy_scope_id: str) -> ActivePolicySet:
        """
        Retorna o ActivePolicySet para o escopo solicitado.

        Em steady state (escopo já inicializado): retorna o conjunto em
        memória sem nenhum I/O — leitura de dicionário.

        Se o escopo ainda não foi inicializado (cold start para este escopo):
        tenta fazer refresh. Se o refresh falhar e não houver LKG disponível,
        levanta PolicyEngineNotReady.

        Args:
            policy_scope_id: identificador do escopo de policy.

        Returns:
            ActivePolicySet ativo para o escopo.

        Raises:
            PolicyEngineNotReady: se não houver ActivePolicySet válido para
                                  o escopo e o bootstrap falhar sem LKG disponível.
        """
        # Hot path: retornar do cache em memória sem I/O.
        if policy_scope_id in self._active_sets:
            return self._active_sets[policy_scope_id]

        # Escopo não inicializado — tentar bootstrap.
        logger.info(
            "escopo não inicializado — tentando bootstrap",
            extra={"policy_scope_id": policy_scope_id},
        )
        self.refresh_scope(policy_scope_id)

        # Após refresh, o escopo deve estar no cache.
        # Se ainda não estiver, o refresh falhou e PolicyEngineNotReady foi levantado.
        return self._active_sets[policy_scope_id]

    def refresh_scope(self, policy_scope_id: str) -> None:
        """
        Atualiza o ActivePolicySet para o escopo a partir do manifesto ativo.

        Fluxo de refresh (Requisito 6.4):
        1. Resolver manifesto ativo do escopo via AppConfig.
        2. Comparar activation_id com o conjunto atual em memória.
        3. Se mudou (ou não há conjunto atual): carregar bundle e snapshot.
        4. Verificar integridade e compatibilidade.
        5. Construir novo ActivePolicySet.
        6. Salvar no LKGStore.
        7. Marcar boot válido no LKGStore (na primeira vez).
        8. Trocar ActivePolicySet por swap atômico.

        Em caso de falha:
        - Se há LKG disponível (boot válido prévio): usar LKG e logar warning.
        - Se não há LKG (cold start): levantar PolicyEngineNotReady.

        Args:
            policy_scope_id: identificador do escopo a atualizar.

        Raises:
            PolicyEngineNotReady: se o refresh falhar e não houver LKG disponível.
        """
        logger.info(
            "iniciando refresh do escopo",
            extra={
                "policy_scope_id": policy_scope_id,
                "evaluator_version": self._evaluator_version,
            },
        )

        try:
            new_active_set = self._load_active_policy_set(policy_scope_id)
        except ValidationEngineError as error:
            # Refresh falhou — tentar usar Last Known Good.
            self._handle_refresh_failure(policy_scope_id, error)
            return

        # Refresh bem-sucedido — salvar LKG e fazer swap atômico.
        self._lkg_store.save(policy_scope_id, new_active_set)

        # Marcar boot válido na primeira inicialização bem-sucedida.
        # Após esta chamada, o LKG fica disponível para uso como fallback.
        if not self._lkg_store.has_valid_boot:
            self._lkg_store.mark_boot_valid()

        # Swap atômico: substituição de referência no dicionário.
        # Em CPython, atribuição de referência é atômica pelo GIL.
        self._active_sets[policy_scope_id] = new_active_set

        logger.info(
            "refresh do escopo concluído com sucesso",
            extra={
                "policy_scope_id": policy_scope_id,
                "activation_id": new_active_set.manifest.activation_id,
                "artifact_hash": new_active_set.manifest.artifact_hash,
                "snapshot_version": new_active_set.manifest.snapshot_version,
            },
        )

    def get_current_activation_id(self, policy_scope_id: str) -> str | None:
        """
        Retorna o activation_id do conjunto ativo para o escopo, se disponível.

        Usado internamente para detectar se o manifesto mudou e um refresh
        completo é necessário.

        Args:
            policy_scope_id: identificador do escopo.

        Returns:
            activation_id do conjunto ativo, ou None se o escopo não está
            inicializado.
        """
        active_set = self._active_sets.get(policy_scope_id)
        if active_set is None:
            return None
        return active_set.manifest.activation_id

    def _load_active_policy_set(self, policy_scope_id: str) -> ActivePolicySet:
        """
        Carrega um novo ActivePolicySet a partir do manifesto ativo.

        Executa o fluxo completo de carregamento:
        1. Resolver manifesto do AppConfig.
        2. Verificar se o activation_id mudou (skip se igual).
        3. Carregar bundle do S3 via BundleLoader.
        4. Carregar snapshot do S3 via SnapshotLoader.
        5. Construir ActivePolicySet com integrity_verified=True.

        Args:
            policy_scope_id: identificador do escopo.

        Returns:
            Novo ActivePolicySet carregado e verificado.

        Raises:
            PolicyBundleUnavailable:      se o bundle não puder ser carregado.
            PolicySnapshotUnavailable:    se o snapshot não puder ser carregado.
            PolicyBundleIntegrityFailure: se a integridade do bundle falhar.
            InvalidPolicyBundle:          se o bundle for incompatível.
        """
        # Passo 1: Resolver manifesto ativo do AppConfig.
        manifest = self._manifest_resolver.resolve(policy_scope_id)

        # Passo 2: Verificar se o activation_id mudou.
        # Se o manifesto não mudou, o ActivePolicySet atual ainda é válido.
        current_activation_id = self.get_current_activation_id(policy_scope_id)
        if (
            current_activation_id is not None
            and current_activation_id == manifest.activation_id
        ):
            logger.debug(
                "manifesto não mudou — ActivePolicySet atual ainda válido",
                extra={
                    "policy_scope_id": policy_scope_id,
                    "activation_id": manifest.activation_id,
                },
            )
            # Retornar o conjunto atual sem recarregar artefatos.
            return self._active_sets[policy_scope_id]

        logger.info(
            "novo manifesto detectado — carregando artefatos",
            extra={
                "policy_scope_id": policy_scope_id,
                "new_activation_id": manifest.activation_id,
                "previous_activation_id": current_activation_id,
                "artifact_hash": manifest.artifact_hash,
                "snapshot_version": manifest.snapshot_version,
            },
        )

        # Passo 3: Carregar bundle do S3.
        # BundleLoader verifica integridade (SHA-256) e compatibilidade.
        bundle = self._bundle_loader.load(manifest.artifact_hash)

        # Passo 4: Carregar snapshot do S3.
        # SnapshotLoader verifica compatibilidade de schema.
        snapshot = self._snapshot_loader.load(manifest.snapshot_version)

        # Passo 5: Construir ActivePolicySet.
        # integrity_verified=True porque BundleLoader já verificou o hash.
        loaded_at = datetime.now(tz=timezone.utc).isoformat()
        return ActivePolicySet(
            manifest=manifest,
            bundle=bundle,
            snapshot=snapshot,
            loaded_at=loaded_at,
            integrity_verified=True,
        )

    def _handle_refresh_failure(
        self,
        policy_scope_id: str,
        error: ValidationEngineError,
    ) -> None:
        """
        Trata falha de refresh tentando usar o Last Known Good.

        Política de degradação (Requisito 17.1, 17.2):
        - Se há LKG disponível (boot válido prévio): usar LKG e logar warning.
        - Se não há LKG (cold start): levantar PolicyEngineNotReady.

        Args:
            policy_scope_id: identificador do escopo que falhou.
            error:           erro que causou a falha de refresh.

        Raises:
            PolicyEngineNotReady: se não há LKG disponível para fallback.
        """
        logger.warning(
            "falha no refresh do escopo — tentando Last Known Good",
            extra={
                "policy_scope_id": policy_scope_id,
                "error_code": getattr(error, "code", "UNKNOWN"),
                "error_message": str(error),
            },
        )

        # Tentar carregar o Last Known Good do disco.
        lkg = self._lkg_store.load(policy_scope_id)

        if lkg is not None:
            # LKG disponível — usar como fallback e continuar operando.
            # O swap atômico garante que o hot path veja o LKG imediatamente.
            self._active_sets[policy_scope_id] = lkg

            logger.warning(
                "usando Last Known Good como fallback após falha de refresh",
                extra={
                    "policy_scope_id": policy_scope_id,
                    "lkg_activation_id": lkg.manifest.activation_id,
                    "lkg_artifact_hash": lkg.manifest.artifact_hash,
                    "lkg_loaded_at": lkg.loaded_at,
                },
            )
            return

        # Sem LKG disponível — falhar de forma segura (fail-closed).
        # Isso ocorre no cold start quando o primeiro refresh falha.
        logger.error(
            "sem Last Known Good disponível — PolicyEngineNotReady",
            extra={
                "policy_scope_id": policy_scope_id,
                "original_error": str(error),
                "has_valid_boot": self._lkg_store.has_valid_boot,
            },
        )

        raise PolicyEngineNotReady(
            f"Motor de validação sem policy válida para escopo '{policy_scope_id}'. "
            f"Falha de bootstrap: {error}. "
            f"Sem Last Known Good disponível."
        )
