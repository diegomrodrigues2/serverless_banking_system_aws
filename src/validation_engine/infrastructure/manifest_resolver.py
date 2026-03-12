"""
ManifestResolver — resolução de PolicyActivationManifests via AppConfig.

Responsabilidade:
    Resolver o PolicyActivationManifest ativo para um dado policy_scope_id,
    lendo o payload do AppConfig e extraindo o escopo correspondente.

Estratégia de leitura:
    Usa a API AppConfig Agent (StartConfigurationSession + GetLatestConfiguration)
    para obter o manifesto mais recente. O payload é um JSON com a estrutura:

        {
          "version": "1",
          "scopes": {
            "tenantA:TRANSFER:PIX:*:prod": {
              "activation_id": "act_001",
              "artifact_hash": "sha256:...",
              "snapshot_version": "snap_001",
              "context_schema_version": "1.0",
              "evaluator_version": "1.2.0"
            }
          }
        }

    O resolver extrai o escopo correspondente ao policy_scope_id solicitado
    e retorna um PolicyActivationManifest tipado.

Múltiplos escopos:
    Um único payload AppConfig pode conter múltiplos escopos. O resolver
    suporta lookup por scope_id exato. Escopos ausentes levantam
    PolicyBundleUnavailable para que o runtime falhe de forma controlada.

Validação estrutural:
    O payload é validado estruturalmente antes de ser parseado. Campos
    obrigatórios ausentes levantam InvalidPolicyBundle com detalhes do campo
    faltante para facilitar diagnóstico.

Cache de sessão:
    O resolver mantém o session_token do AppConfig para evitar polling
    desnecessário. O AppConfig retorna payload vazio quando não há mudanças
    desde a última leitura (304 implícito via token).

Requisitos cobertos: 4.3, 4.4, 5.1, 5.3
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import botocore.exceptions

from validation_engine.domain.errors import (
    InvalidPolicyBundle,
    PolicyBundleUnavailable,
)
from validation_engine.domain.models import PolicyActivationManifest

if TYPE_CHECKING:
    from mypy_boto3_appconfig import AppConfigClient
    from mypy_boto3_appconfigdata import AppConfigDataClient

logger = logging.getLogger(__name__)

# Campos obrigatórios em cada entrada de escopo no payload do manifesto.
# Validados antes de construir o PolicyActivationManifest para garantir
# que o Data Plane nunca receba um manifesto incompleto.
_REQUIRED_SCOPE_FIELDS = frozenset({
    "activation_id",
    "artifact_hash",
    "snapshot_version",
    "context_schema_version",
    "evaluator_version",
})


class ManifestResolver:
    """
    Resolve PolicyActivationManifests a partir do AppConfig.

    Lê o payload de configuração do AppConfig, valida a estrutura e extrai
    o manifesto ativo para o policy_scope_id solicitado.

    O resolver mantém um session_token por instância para aproveitar o
    mecanismo de polling eficiente do AppConfig (retorna payload vazio
    quando não há mudanças desde a última leitura).

    Uso típico (PolicyRuntimeRegistry):
        resolver = ManifestResolver(
            appconfig_data_client=appconfig_data,
            application_id="ledger-validation-engine-dev",
            environment_id="dev",
            configuration_profile_id="policy-activation-manifests",
        )
        manifest = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

    Thread safety:
        Esta implementação NÃO é thread-safe. O PolicyRuntimeRegistry deve
        garantir acesso serializado ao resolver ou usar instâncias separadas
        por thread.
    """

    def __init__(
        self,
        appconfig_data_client: "AppConfigDataClient",
        application_id: str,
        environment_id: str,
        configuration_profile_id: str,
    ) -> None:
        """
        Inicializa o ManifestResolver.

        Args:
            appconfig_data_client:      cliente boto3 AppConfigData (appconfigdata).
            application_id:             ID da AppConfig Application.
            environment_id:             ID do AppConfig Environment.
            configuration_profile_id:   ID do AppConfig Configuration Profile.
        """
        self._client = appconfig_data_client
        self._application_id = application_id
        self._environment_id = environment_id
        self._configuration_profile_id = configuration_profile_id

        # Token de sessão AppConfig — mantido entre chamadas para polling eficiente.
        # None indica que a sessão ainda não foi iniciada.
        self._session_token: str | None = None

        # Último payload completo recebido do AppConfig.
        # Reutilizado quando o AppConfig retorna payload vazio (sem mudanças).
        self._last_payload: dict | None = None

    def resolve(self, policy_scope_id: str) -> PolicyActivationManifest:
        """
        Resolve o PolicyActivationManifest ativo para um policy_scope_id.

        Fluxo:
        1. Iniciar sessão AppConfig se ainda não iniciada.
        2. Obter configuração mais recente (payload vazio = sem mudanças).
        3. Atualizar cache interno se payload novo recebido.
        4. Extrair escopo do payload em cache.
        5. Validar campos obrigatórios do escopo.
        6. Construir e retornar PolicyActivationManifest.

        Args:
            policy_scope_id: identificador do escopo (ex: "tenantA:TRANSFER:PIX:*:prod").

        Returns:
            PolicyActivationManifest ativo para o escopo solicitado.

        Raises:
            PolicyBundleUnavailable: se o escopo não existir no manifesto ou
                                     se ocorrer erro de I/O com o AppConfig.
            InvalidPolicyBundle:     se o payload do manifesto for inválido
                                     ou campos obrigatórios estiverem ausentes.
        """
        # Garantir que a sessão AppConfig está ativa antes de tentar ler.
        self._ensure_session()

        # Obter configuração mais recente do AppConfig.
        # Retorna payload vazio se não houve mudanças desde a última leitura.
        raw_payload = self._fetch_latest_configuration()

        if raw_payload:
            # Novo payload recebido — parsear e atualizar cache interno.
            self._last_payload = self._parse_and_validate_payload(raw_payload)
            logger.info(
                "manifesto atualizado recebido do AppConfig",
                extra={
                    "application_id": self._application_id,
                    "environment_id": self._environment_id,
                    "scopes_count": len(self._last_payload.get("scopes", {})),
                },
            )
        elif self._last_payload is None:
            # Primeira leitura retornou payload vazio — AppConfig sem configuração publicada.
            raise PolicyBundleUnavailable(
                f"AppConfig não retornou manifesto para application='{self._application_id}', "
                f"environment='{self._environment_id}'. Verifique se um manifesto foi publicado."
            )
        else:
            # Payload vazio com cache existente — sem mudanças desde a última leitura.
            logger.debug(
                "AppConfig sem mudanças — usando manifesto em cache",
                extra={"policy_scope_id": policy_scope_id},
            )

        # Extrair o escopo solicitado do payload em cache.
        return self._extract_scope_manifest(policy_scope_id)

    def invalidate_session(self) -> None:
        """
        Invalida a sessão AppConfig e o cache de payload.

        Força uma nova sessão e re-leitura completa do manifesto na próxima
        chamada a resolve(). Útil para forçar refresh após falha de rede
        ou para testes que precisam de estado limpo.
        """
        self._session_token = None
        self._last_payload = None
        logger.info(
            "sessão AppConfig invalidada — próxima leitura iniciará nova sessão",
            extra={
                "application_id": self._application_id,
                "environment_id": self._environment_id,
            },
        )

    def _ensure_session(self) -> None:
        """
        Garante que a sessão AppConfig está ativa.

        Inicia uma nova sessão se o token ainda não existe.
        O token é reutilizado entre chamadas para aproveitar o polling
        eficiente do AppConfig.

        Raises:
            PolicyBundleUnavailable: se ocorrer erro ao iniciar a sessão.
        """
        if self._session_token is not None:
            return

        try:
            response = self._client.start_configuration_session(
                ApplicationIdentifier=self._application_id,
                EnvironmentIdentifier=self._environment_id,
                ConfigurationProfileIdentifier=self._configuration_profile_id,
            )
            self._session_token = response["InitialConfigurationToken"]
            logger.info(
                "sessão AppConfig iniciada",
                extra={
                    "application_id": self._application_id,
                    "environment_id": self._environment_id,
                    "configuration_profile_id": self._configuration_profile_id,
                },
            )
        except botocore.exceptions.ClientError as error:
            raise PolicyBundleUnavailable(
                f"Falha ao iniciar sessão AppConfig para application='{self._application_id}': {error}"
            ) from error

    def _fetch_latest_configuration(self) -> str:
        """
        Obtém a configuração mais recente do AppConfig.

        Usa o session_token para polling eficiente. O AppConfig retorna:
        - payload não-vazio: nova configuração disponível
        - payload vazio:     sem mudanças desde a última leitura

        O token é atualizado a cada chamada (AppConfig rotaciona o token).

        Returns:
            String JSON do payload (pode ser vazia se sem mudanças).

        Raises:
            PolicyBundleUnavailable: se ocorrer erro de I/O com o AppConfig.
        """
        try:
            response = self._client.get_latest_configuration(
                ConfigurationToken=self._session_token,
            )
            # AppConfig rotaciona o token a cada chamada — atualizar para próxima leitura.
            self._session_token = response["NextPollConfigurationToken"]

            # Ler o conteúdo do streaming body.
            raw_bytes = response["Configuration"].read()
            return raw_bytes.decode("utf-8") if raw_bytes else ""

        except botocore.exceptions.ClientError as error:
            # Token expirado ou inválido — invalidar sessão para forçar nova inicialização.
            error_code = error.response.get("Error", {}).get("Code", "")
            if error_code in ("BadRequestException", "ResourceNotFoundException"):
                logger.warning(
                    "token AppConfig inválido ou expirado — invalidando sessão",
                    extra={"error_code": error_code},
                )
                self.invalidate_session()
            raise PolicyBundleUnavailable(
                f"Falha ao obter configuração do AppConfig: {error}"
            ) from error

    def _parse_and_validate_payload(self, raw_payload: str) -> dict:
        """
        Parseia e valida a estrutura do payload do manifesto.

        Validações realizadas:
        1. JSON válido.
        2. Campo "version" presente e igual a "1".
        3. Campo "scopes" presente e é um dicionário.

        Args:
            raw_payload: string JSON do payload do AppConfig.

        Returns:
            Dicionário parseado e validado.

        Raises:
            InvalidPolicyBundle: se o payload for inválido ou mal-formado.
        """
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as error:
            raise InvalidPolicyBundle(
                f"Payload do manifesto AppConfig não é JSON válido: {error}"
            ) from error

        # Validar campo "version" — garante compatibilidade com o schema esperado.
        version = payload.get("version")
        if version != "1":
            raise InvalidPolicyBundle(
                f"Versão do manifesto AppConfig inválida: esperado '1', recebido '{version}'"
            )

        # Validar campo "scopes" — deve ser um dicionário de escopos.
        scopes = payload.get("scopes")
        if not isinstance(scopes, dict):
            raise InvalidPolicyBundle(
                f"Campo 'scopes' do manifesto AppConfig deve ser um objeto JSON, "
                f"recebido: {type(scopes).__name__}"
            )

        return payload

    def _extract_scope_manifest(self, policy_scope_id: str) -> PolicyActivationManifest:
        """
        Extrai e valida o manifesto de um escopo específico do payload em cache.

        Args:
            policy_scope_id: identificador do escopo a extrair.

        Returns:
            PolicyActivationManifest construído a partir dos dados do escopo.

        Raises:
            PolicyBundleUnavailable: se o escopo não existir no manifesto.
            InvalidPolicyBundle:     se campos obrigatórios estiverem ausentes.
        """
        scopes: dict = self._last_payload.get("scopes", {})

        scope_data = scopes.get(policy_scope_id)
        if scope_data is None:
            available_scopes = list(scopes.keys())
            raise PolicyBundleUnavailable(
                f"Escopo '{policy_scope_id}' não encontrado no manifesto AppConfig. "
                f"Escopos disponíveis: {available_scopes}"
            )

        # Validar campos obrigatórios do escopo antes de construir o manifesto.
        self._validate_scope_fields(policy_scope_id, scope_data)

        # Construir o PolicyActivationManifest tipado a partir dos dados do escopo.
        # activated_at e activated_by são derivados do contexto de resolução
        # quando não presentes no payload (campos opcionais no payload AppConfig).
        return PolicyActivationManifest(
            activation_id=scope_data["activation_id"],
            policy_scope_id=policy_scope_id,
            artifact_hash=scope_data["artifact_hash"],
            snapshot_version=scope_data["snapshot_version"],
            context_schema_version=scope_data["context_schema_version"],
            evaluator_version=scope_data["evaluator_version"],
            activated_at=scope_data.get(
                "activated_at",
                datetime.now(tz=timezone.utc).isoformat(),
            ),
            activated_by=scope_data.get("activated_by", "appconfig"),
        )

    def _validate_scope_fields(self, policy_scope_id: str, scope_data: dict) -> None:
        """
        Valida que todos os campos obrigatórios estão presentes no escopo.

        Args:
            policy_scope_id: identificador do escopo (para mensagens de erro).
            scope_data:      dados do escopo a validar.

        Raises:
            InvalidPolicyBundle: se algum campo obrigatório estiver ausente ou vazio.
        """
        for field_name in _REQUIRED_SCOPE_FIELDS:
            value = scope_data.get(field_name)
            if not value:
                raise InvalidPolicyBundle(
                    f"Campo obrigatório '{field_name}' ausente ou vazio no escopo "
                    f"'{policy_scope_id}' do manifesto AppConfig"
                )
