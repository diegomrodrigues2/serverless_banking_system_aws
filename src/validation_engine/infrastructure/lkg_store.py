"""
LKGStore — persistência local do Last Known Good (LKG) ActivePolicySet.

Responsabilidade:
    Salvar e carregar o último ActivePolicySet válido em disco (por padrão
    em /tmp ou diretório configurável), permitindo que o PolicyRuntimeRegistry
    use o LKG em caso de falha de refresh após uma inicialização bem-sucedida.

Semântica de segurança (Requisito 17.2):
    O LKG só pode ser usado APÓS ao menos uma inicialização bem-sucedida.
    O LKGStore controla este invariante via flag `has_valid_boot`:
    - Antes do primeiro boot válido: load() retorna None (não há LKG)
    - Após o primeiro boot válido: load() retorna o último ActivePolicySet salvo

    Isso garante que o sistema nunca use um LKG de uma execução anterior
    sem ter validado o estado atual do runtime pelo menos uma vez.

Formato de persistência:
    O ActivePolicySet é serializado em JSON UTF-8 e salvo em um arquivo
    no diretório configurável. O nome do arquivo é derivado do policy_scope_id
    para suportar múltiplos escopos independentes.

    Estrutura do arquivo:
        {lkg_dir}/{scope_id_sanitized}.lkg.json

    O scope_id é sanitizado (substituindo ':' por '_') para ser um nome
    de arquivo válido em todos os sistemas operacionais.

Thread safety:
    Esta implementação NÃO é thread-safe. O PolicyRuntimeRegistry deve
    garantir acesso serializado ao LKGStore ou usar instâncias separadas
    por escopo.

Requisitos cobertos: 17.2
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from validation_engine.domain.models import (
    ActivePolicySet,
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
    RuleBundle,
)

logger = logging.getLogger(__name__)

# Diretório padrão para armazenamento do LKG.
# Usa /tmp para garantir disponibilidade em ambientes Lambda e containers.
_DEFAULT_LKG_DIR = "/tmp/validation_engine_lkg"


class LKGStore:
    """
    Armazenamento local do Last Known Good (LKG) ActivePolicySet.

    Persiste o último ActivePolicySet válido em disco para uso em caso
    de falha de refresh. O LKG só é disponibilizado após ao menos uma
    inicialização bem-sucedida do runtime (boot válido).

    Uso típico (PolicyRuntimeRegistry):
        lkg_store = LKGStore(lkg_dir="/tmp/validation_engine_lkg")

        # Após bootstrap bem-sucedido:
        lkg_store.save(scope_id, active_policy_set)
        lkg_store.mark_boot_valid()

        # Em caso de falha de refresh:
        lkg = lkg_store.load(scope_id)
        if lkg is not None:
            # usar LKG
        else:
            # falhar com PolicyEngineNotReady (sem boot válido prévio)
    """

    def __init__(self, lkg_dir: str = _DEFAULT_LKG_DIR) -> None:
        """
        Inicializa o LKGStore.

        Args:
            lkg_dir: diretório onde os arquivos LKG serão armazenados.
                     Criado automaticamente se não existir.
                     Padrão: /tmp/validation_engine_lkg
        """
        self._lkg_dir = Path(lkg_dir)
        # Flag de controle: True somente após ao menos um boot válido.
        # Garante que o LKG nunca seja usado antes de uma inicialização bem-sucedida.
        self._has_valid_boot: bool = False

        # Criar o diretório de armazenamento se não existir.
        # exist_ok=True evita race condition em inicializações paralelas.
        self._lkg_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(
            "LKGStore inicializado",
            extra={"lkg_dir": str(self._lkg_dir)},
        )

    @property
    def has_valid_boot(self) -> bool:
        """
        Indica se houve ao menos uma inicialização bem-sucedida.

        Retorna True somente após mark_boot_valid() ter sido chamado.
        Controla se o LKG pode ser usado como fallback.
        """
        return self._has_valid_boot

    def mark_boot_valid(self) -> None:
        """
        Marca que o runtime teve ao menos uma inicialização bem-sucedida.

        Deve ser chamado pelo PolicyRuntimeRegistry após o primeiro
        bootstrap bem-sucedido (bundle + snapshot carregados e verificados).

        Após esta chamada, load() passa a retornar o LKG salvo em vez de None.
        """
        if not self._has_valid_boot:
            self._has_valid_boot = True
            logger.info(
                "boot válido registrado — LKG disponível para uso como fallback",
                extra={"lkg_dir": str(self._lkg_dir)},
            )

    def save(self, scope_id: str, active_policy_set: ActivePolicySet) -> None:
        """
        Salva o ActivePolicySet como Last Known Good para o escopo.

        Serializa o conjunto em JSON e persiste em disco. Sobrescreve
        qualquer LKG anterior para o mesmo escopo.

        Args:
            scope_id:          identificador do escopo (ex: "tenantA:TRANSFER:PIX:*:prod").
            active_policy_set: conjunto de policy a persistir como LKG.
        """
        file_path = self._lkg_file_path(scope_id)

        try:
            payload = _serialize_active_policy_set(active_policy_set)
            json_content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            file_path.write_text(json_content, encoding="utf-8")

            logger.info(
                "LKG salvo com sucesso",
                extra={
                    "scope_id": scope_id,
                    "activation_id": active_policy_set.manifest.activation_id,
                    "artifact_hash": active_policy_set.manifest.artifact_hash,
                    "file_path": str(file_path),
                },
            )
        except OSError as error:
            # Falha ao salvar o LKG não deve interromper o fluxo principal.
            # O runtime continua funcionando; apenas o fallback fica indisponível.
            logger.error(
                "falha ao salvar LKG em disco — fallback indisponível para este escopo",
                extra={
                    "scope_id": scope_id,
                    "file_path": str(file_path),
                    "error": str(error),
                },
            )

    def load(self, scope_id: str) -> ActivePolicySet | None:
        """
        Carrega o Last Known Good para o escopo, se disponível.

        Retorna None em dois casos:
        1. Nunca houve boot válido (has_valid_boot == False).
        2. Não existe arquivo LKG para o escopo.

        Isso garante que o LKG nunca seja usado antes de uma inicialização
        bem-sucedida, mesmo que exista um arquivo de uma execução anterior.

        Args:
            scope_id: identificador do escopo.

        Returns:
            ActivePolicySet do LKG, ou None se indisponível.
        """
        # Invariante de segurança: LKG só disponível após boot válido.
        # Sem boot válido, retornar None força o runtime a falhar com
        # PolicyEngineNotReady em vez de usar estado potencialmente obsoleto.
        if not self._has_valid_boot:
            logger.debug(
                "LKG solicitado antes de boot válido — retornando None",
                extra={"scope_id": scope_id},
            )
            return None

        file_path = self._lkg_file_path(scope_id)

        if not file_path.exists():
            logger.debug(
                "arquivo LKG não encontrado para o escopo",
                extra={"scope_id": scope_id, "file_path": str(file_path)},
            )
            return None

        try:
            json_content = file_path.read_text(encoding="utf-8")
            payload = json.loads(json_content)
            active_policy_set = _deserialize_active_policy_set(payload)

            logger.info(
                "LKG carregado com sucesso",
                extra={
                    "scope_id": scope_id,
                    "activation_id": active_policy_set.manifest.activation_id,
                    "artifact_hash": active_policy_set.manifest.artifact_hash,
                    "file_path": str(file_path),
                },
            )
            return active_policy_set

        except (OSError, json.JSONDecodeError, KeyError, ValueError) as error:
            # Arquivo corrompido ou estrutura inesperada — tratar como ausente.
            # Logar o erro para diagnóstico mas não propagar a exceção.
            logger.error(
                "falha ao carregar LKG — arquivo corrompido ou inválido",
                extra={
                    "scope_id": scope_id,
                    "file_path": str(file_path),
                    "error": str(error),
                },
            )
            return None

    def clear(self, scope_id: str) -> None:
        """
        Remove o arquivo LKG para o escopo.

        Usado em testes e em cenários de limpeza explícita.
        Não levanta exceção se o arquivo não existir.

        Args:
            scope_id: identificador do escopo.
        """
        file_path = self._lkg_file_path(scope_id)
        try:
            file_path.unlink(missing_ok=True)
            logger.debug(
                "arquivo LKG removido",
                extra={"scope_id": scope_id, "file_path": str(file_path)},
            )
        except OSError as error:
            logger.warning(
                "falha ao remover arquivo LKG",
                extra={"scope_id": scope_id, "error": str(error)},
            )

    def _lkg_file_path(self, scope_id: str) -> Path:
        """
        Retorna o caminho do arquivo LKG para o escopo.

        O scope_id é sanitizado substituindo ':' por '_' para produzir
        um nome de arquivo válido em todos os sistemas operacionais.

        Exemplo:
            scope_id = "tenantA:TRANSFER:PIX:*:prod"
            → /tmp/validation_engine_lkg/tenantA_TRANSFER_PIX___prod.lkg.json

        Args:
            scope_id: identificador do escopo.

        Returns:
            Path do arquivo LKG.
        """
        # Sanitizar o scope_id para uso como nome de arquivo.
        # ':' e '*' são substituídos por '_' para compatibilidade universal.
        sanitized = scope_id.replace(":", "_").replace("*", "_")
        return self._lkg_dir / f"{sanitized}.lkg.json"


# ---------------------------------------------------------------------------
# Helpers de serialização/desserialização do ActivePolicySet
# ---------------------------------------------------------------------------


def _serialize_active_policy_set(aps: ActivePolicySet) -> dict:
    """
    Serializa um ActivePolicySet para dicionário persistível em JSON.

    Inclui todos os campos necessários para reconstrução completa,
    incluindo manifesto, bundle e snapshot.

    Args:
        aps: ActivePolicySet a serializar.

    Returns:
        Dicionário serializável em JSON.
    """
    from validation_engine.domain.models import _ast_to_dict

    manifest = aps.manifest
    bundle = aps.bundle
    snapshot = aps.snapshot

    # Serializar dados do snapshot — converter tuples para listas para JSON.
    serialized_data: dict = {}
    for key, value in snapshot.data.items():
        serialized_data[key] = list(value) if isinstance(value, tuple) else value

    return {
        "saved_at": datetime.now(tz=timezone.utc).isoformat(),
        "manifest": {
            "activation_id": manifest.activation_id,
            "policy_scope_id": manifest.policy_scope_id,
            "artifact_hash": manifest.artifact_hash,
            "snapshot_version": manifest.snapshot_version,
            "context_schema_version": manifest.context_schema_version,
            "evaluator_version": manifest.evaluator_version,
            "activated_at": manifest.activated_at,
            "activated_by": manifest.activated_by,
        },
        "bundle": {
            "policy_set_id": bundle.policy_set_id,
            "artifact_hash": bundle.artifact_hash,
            "ast": _ast_to_dict(bundle.ast),
            "execution_plan": bundle.execution_plan,
            "compatibility": {
                "dsl_version": bundle.compatibility.dsl_version,
                "context_schema_version": bundle.compatibility.context_schema_version,
                "snapshot_schema_version": bundle.compatibility.snapshot_schema_version,
                "evaluator_min_version": bundle.compatibility.evaluator_min_version,
            },
            "composition_mode": bundle.composition_mode.value,
            "metadata": {
                "author": bundle.metadata.author,
                "description": bundle.metadata.description,
                "compiled_at": bundle.metadata.compiled_at,
                "source_hash": bundle.metadata.source_hash,
            },
        },
        "snapshot": {
            "snapshot_version": snapshot.snapshot_version,
            "snapshot_schema_version": snapshot.snapshot_schema_version,
            "created_at": snapshot.created_at,
            "data": serialized_data,
        },
        "loaded_at": aps.loaded_at,
        "integrity_verified": aps.integrity_verified,
    }


def _deserialize_active_policy_set(payload: dict) -> ActivePolicySet:
    """
    Reconstrói um ActivePolicySet a partir de dicionário desserializado.

    Restaura todos os tipos corretos, incluindo tuples do snapshot.

    Args:
        payload: dicionário carregado do arquivo JSON.

    Returns:
        ActivePolicySet reconstruído.

    Raises:
        KeyError:   se campos obrigatórios estiverem ausentes.
        ValueError: se valores inválidos forem encontrados.
    """
    from validation_engine.domain.models import _ast_from_dict
    from validation_engine.domain.policy_ast import CompositionMode

    manifest_raw = payload["manifest"]
    bundle_raw = payload["bundle"]
    snapshot_raw = payload["snapshot"]

    manifest = PolicyActivationManifest(
        activation_id=manifest_raw["activation_id"],
        policy_scope_id=manifest_raw["policy_scope_id"],
        artifact_hash=manifest_raw["artifact_hash"],
        snapshot_version=manifest_raw["snapshot_version"],
        context_schema_version=manifest_raw["context_schema_version"],
        evaluator_version=manifest_raw["evaluator_version"],
        activated_at=manifest_raw["activated_at"],
        activated_by=manifest_raw["activated_by"],
    )

    compat_raw = bundle_raw["compatibility"]
    meta_raw = bundle_raw["metadata"]
    bundle = RuleBundle(
        policy_set_id=bundle_raw["policy_set_id"],
        artifact_hash=bundle_raw["artifact_hash"],
        ast=_ast_from_dict(bundle_raw["ast"]),
        execution_plan=bundle_raw["execution_plan"],
        compatibility=BundleCompatibility(
            dsl_version=compat_raw["dsl_version"],
            context_schema_version=compat_raw["context_schema_version"],
            snapshot_schema_version=compat_raw["snapshot_schema_version"],
            evaluator_min_version=compat_raw["evaluator_min_version"],
        ),
        composition_mode=CompositionMode(bundle_raw["composition_mode"]),
        metadata=CompilationMetadata(
            author=meta_raw["author"],
            description=meta_raw["description"],
            compiled_at=meta_raw["compiled_at"],
            source_hash=meta_raw["source_hash"],
        ),
    )

    # Restaurar tipos do snapshot: listas JSON → tuples Python.
    restored_data: dict = {}
    for key, value in snapshot_raw["data"].items():
        if isinstance(value, list):
            if len(value) == 0:
                restored_data[key] = ()
            elif isinstance(value[0], str):
                restored_data[key] = tuple(str(v) for v in value)
            elif isinstance(value[0], int):
                restored_data[key] = tuple(int(v) for v in value)
            else:
                restored_data[key] = tuple(value)
        else:
            restored_data[key] = value

    snapshot = ReferenceSnapshot(
        snapshot_version=snapshot_raw["snapshot_version"],
        snapshot_schema_version=snapshot_raw["snapshot_schema_version"],
        created_at=snapshot_raw["created_at"],
        data=restored_data,
    )

    return ActivePolicySet(
        manifest=manifest,
        bundle=bundle,
        snapshot=snapshot,
        loaded_at=payload["loaded_at"],
        integrity_verified=payload["integrity_verified"],
    )
