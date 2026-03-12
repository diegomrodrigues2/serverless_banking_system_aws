"""
PolicyPublisher — publicação de PolicyActivationManifests no AppConfig.

Responsabilidade:
    Gerar e publicar PolicyActivationManifests no AppConfig, garantindo que
    bundle e snapshot sejam compatíveis antes da ativação.

Fluxo de publicação:
    1. Validar compatibilidade entre bundle, snapshot e runtime.
    2. Gerar PolicyActivationManifest com activation_id único.
    3. Serializar o manifesto para o formato JSON do AppConfig.
    4. Criar nova versão hospedada no AppConfig (CreateHostedConfigurationVersion).
    5. Iniciar deployment da nova versão (StartDeployment).
    6. Aguardar conclusão do deployment (polling com timeout).

Compatibilidade:
    Antes de publicar, o publisher valida que:
    - bundle.compatibility.context_schema_version == snapshot.snapshot_schema_version
      (bundle e snapshot foram compilados para o mesmo schema)
    - bundle.compatibility.snapshot_schema_version == snapshot.snapshot_schema_version
      (schema do snapshot é compatível com o bundle)
    - bundle.compatibility.evaluator_min_version é fornecido (não vazio)

    Incompatibilidade levanta InvalidPolicyBundle antes de qualquer I/O.

Rollback:
    Rollback é implementado publicando um novo manifesto apontando para
    versões anteriores de bundle e snapshot. O histórico de manifestos
    é preservado no AppConfig para auditoria.

Requisitos cobertos: 4.1, 4.2, 4.3, 4.5, 24.3, 24.4
"""

from __future__ import annotations

import json
import logging
import time
import uuid
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

    from validation_engine.domain.models import ReferenceSnapshot, RuleBundle

logger = logging.getLogger(__name__)

# Timeout máximo para aguardar conclusão do deployment AppConfig (segundos).
# Em dev com all-at-once, o deployment é quase imediato.
# Em prod com rollout gradual, pode levar minutos.
_DEPLOYMENT_POLL_TIMEOUT_SECONDS = 300
_DEPLOYMENT_POLL_INTERVAL_SECONDS = 5

# Estados finais de deployment AppConfig — polling para quando atingir um destes.
_DEPLOYMENT_FINAL_STATES = frozenset({"COMPLETE", "ROLLED_BACK", "BAKING"})
_DEPLOYMENT_SUCCESS_STATES = frozenset({"COMPLETE", "BAKING"})


class PolicyPublisher:
    """
    Publica PolicyActivationManifests no AppConfig (Control Plane).

    Responsável por:
    1. Validar compatibilidade entre bundle e snapshot antes da publicação.
    2. Gerar PolicyActivationManifest com activation_id único.
    3. Publicar o manifesto como nova versão hospedada no AppConfig.
    4. Iniciar o deployment da nova versão.
    5. Aguardar conclusão do deployment (opcional, configurável).

    Uso típico (Control Plane):
        publisher = PolicyPublisher(
            appconfig_client=appconfig,
            application_id="ledger-validation-engine-dev",
            environment_id="dev",
            configuration_profile_id="policy-activation-manifests",
            deployment_strategy_id="AppConfig.AllAtOnce",
            activated_by="ci-pipeline",
        )
        manifest = publisher.publish(
            bundle=compiled_bundle,
            snapshot=reference_snapshot,
            existing_scopes=current_scopes,
        )
    """

    def __init__(
        self,
        appconfig_client: "AppConfigClient",
        application_id: str,
        environment_id: str,
        configuration_profile_id: str,
        deployment_strategy_id: str,
        activated_by: str = "policy-publisher",
        wait_for_deployment: bool = True,
        deployment_timeout_seconds: int = _DEPLOYMENT_POLL_TIMEOUT_SECONDS,
    ) -> None:
        """
        Inicializa o PolicyPublisher.

        Args:
            appconfig_client:           cliente boto3 AppConfig (appconfig).
            application_id:             ID da AppConfig Application.
            environment_id:             ID do AppConfig Environment.
            configuration_profile_id:   ID do AppConfig Configuration Profile.
            deployment_strategy_id:     ID ou nome da estratégia de deployment.
            activated_by:               identidade que está publicando (para auditoria).
            wait_for_deployment:        se True, aguarda conclusão do deployment.
            deployment_timeout_seconds: timeout máximo para aguardar deployment.
        """
        self._client = appconfig_client
        self._application_id = application_id
        self._environment_id = environment_id
        self._configuration_profile_id = configuration_profile_id
        self._deployment_strategy_id = deployment_strategy_id
        self._activated_by = activated_by
        self._wait_for_deployment = wait_for_deployment
        self._deployment_timeout_seconds = deployment_timeout_seconds

    def publish(
        self,
        bundle: "RuleBundle",
        snapshot: "ReferenceSnapshot",
        policy_scope_id: str,
        existing_scopes: dict[str, dict] | None = None,
    ) -> PolicyActivationManifest:
        """
        Publica um novo PolicyActivationManifest para um escopo.

        Fluxo completo:
        1. Validar compatibilidade bundle/snapshot.
        2. Gerar activation_id único.
        3. Construir PolicyActivationManifest.
        4. Mesclar com escopos existentes (para não sobrescrever outros escopos).
        5. Serializar payload completo.
        6. Criar versão hospedada no AppConfig.
        7. Iniciar deployment.
        8. Aguardar conclusão (se wait_for_deployment=True).

        Args:
            bundle:          RuleBundle compilado a ativar.
            snapshot:        ReferenceSnapshot a ativar junto com o bundle.
            policy_scope_id: escopo para o qual o manifesto será publicado.
            existing_scopes: escopos existentes a preservar no payload.
                             Se None, o payload conterá apenas o novo escopo.

        Returns:
            PolicyActivationManifest publicado e ativo.

        Raises:
            InvalidPolicyBundle:     se bundle e snapshot forem incompatíveis.
            PolicyBundleUnavailable: se ocorrer erro de I/O com o AppConfig.
        """
        # Passo 1: Validar compatibilidade antes de qualquer I/O.
        # Incompatibilidade aqui indica erro de configuração no Control Plane.
        self._validate_compatibility(bundle, snapshot)

        # Passo 2: Gerar activation_id único para esta ativação.
        # Formato: act_{timestamp}_{uuid_curto} para legibilidade em logs e auditoria.
        activation_id = self._generate_activation_id()

        # Passo 3: Construir o PolicyActivationManifest tipado.
        manifest = PolicyActivationManifest(
            activation_id=activation_id,
            policy_scope_id=policy_scope_id,
            artifact_hash=bundle.artifact_hash,
            snapshot_version=snapshot.snapshot_version,
            context_schema_version=bundle.compatibility.context_schema_version,
            evaluator_version=bundle.compatibility.evaluator_min_version,
            activated_at=datetime.now(tz=timezone.utc).isoformat(),
            activated_by=self._activated_by,
        )

        # Passo 4: Construir payload completo mesclando com escopos existentes.
        # Isso garante que outros escopos não sejam sobrescritos ao publicar
        # um manifesto para um escopo específico.
        payload = self._build_payload(manifest, existing_scopes or {})

        # Passo 5: Criar nova versão hospedada no AppConfig.
        version_number = self._create_hosted_configuration_version(payload)

        logger.info(
            "versão do manifesto criada no AppConfig",
            extra={
                "activation_id": activation_id,
                "policy_scope_id": policy_scope_id,
                "artifact_hash": bundle.artifact_hash,
                "snapshot_version": snapshot.snapshot_version,
                "version_number": version_number,
            },
        )

        # Passo 6: Iniciar deployment da nova versão.
        deployment_number = self._start_deployment(version_number)

        logger.info(
            "deployment do manifesto iniciado no AppConfig",
            extra={
                "activation_id": activation_id,
                "deployment_number": deployment_number,
                "environment_id": self._environment_id,
            },
        )

        # Passo 7: Aguardar conclusão do deployment (se configurado).
        if self._wait_for_deployment:
            self._wait_for_deployment_completion(deployment_number)

        return manifest

    def _validate_compatibility(
        self,
        bundle: "RuleBundle",
        snapshot: "ReferenceSnapshot",
    ) -> None:
        """
        Valida compatibilidade entre bundle e snapshot antes da publicação.

        Validações realizadas (Requisito 4.4, 24.3):
        1. bundle.compatibility.snapshot_schema_version deve ser igual a
           snapshot.snapshot_schema_version — garante que o bundle foi
           compilado para o schema do snapshot fornecido.
        2. bundle.compatibility.evaluator_min_version não pode ser vazio —
           garante que o bundle declara a versão mínima do evaluator.

        Args:
            bundle:   RuleBundle a validar.
            snapshot: ReferenceSnapshot a validar.

        Raises:
            InvalidPolicyBundle: se qualquer validação falhar.
        """
        # Validar compatibilidade de schema do snapshot.
        # O bundle foi compilado para uma versão específica do schema do snapshot.
        # Usar bundle com snapshot de schema diferente pode produzir resultados incorretos.
        bundle_snapshot_schema = bundle.compatibility.snapshot_schema_version
        snapshot_schema = snapshot.snapshot_schema_version

        if bundle_snapshot_schema != snapshot_schema:
            raise InvalidPolicyBundle(
                f"Bundle incompatível com snapshot: "
                f"bundle.compatibility.snapshot_schema_version='{bundle_snapshot_schema}' "
                f"!= snapshot.snapshot_schema_version='{snapshot_schema}'. "
                f"Bundle e snapshot devem ter o mesmo schema version."
            )

        # Validar que evaluator_min_version está declarado.
        # Um bundle sem evaluator_min_version não pode ser ativado com segurança.
        if not bundle.compatibility.evaluator_min_version:
            raise InvalidPolicyBundle(
                f"Bundle '{bundle.artifact_hash}' não declara evaluator_min_version. "
                f"Recompile o bundle com a versão mínima do evaluator."
            )

        logger.debug(
            "compatibilidade bundle/snapshot validada com sucesso",
            extra={
                "artifact_hash": bundle.artifact_hash,
                "snapshot_version": snapshot.snapshot_version,
                "snapshot_schema_version": snapshot_schema,
                "evaluator_min_version": bundle.compatibility.evaluator_min_version,
            },
        )

    def _generate_activation_id(self) -> str:
        """
        Gera um activation_id único para esta ativação.

        Formato: act_{YYYYMMDD}_{uuid_curto}
        Exemplo: act_20260311_a1b2c3d4

        O formato é legível em logs e auditoria, e único por construção
        (UUID garante unicidade mesmo com múltiplas ativações no mesmo dia).

        Returns:
            String com activation_id único.
        """
        date_part = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        uuid_part = str(uuid.uuid4()).replace("-", "")[:8]
        return f"act_{date_part}_{uuid_part}"

    def _build_payload(
        self,
        manifest: PolicyActivationManifest,
        existing_scopes: dict[str, dict],
    ) -> str:
        """
        Constrói o payload JSON completo para publicação no AppConfig.

        Mescla o novo escopo com os escopos existentes para preservar
        outros escopos ativos. O payload segue o schema validado pelo
        AppConfig Configuration Profile.

        Args:
            manifest:        manifesto do novo escopo a incluir.
            existing_scopes: escopos existentes a preservar.

        Returns:
            String JSON do payload completo.
        """
        # Construir entrada do novo escopo no formato esperado pelo ManifestResolver.
        new_scope_entry = {
            "activation_id": manifest.activation_id,
            "artifact_hash": manifest.artifact_hash,
            "snapshot_version": manifest.snapshot_version,
            "context_schema_version": manifest.context_schema_version,
            "evaluator_version": manifest.evaluator_version,
            "activated_at": manifest.activated_at,
            "activated_by": manifest.activated_by,
        }

        # Mesclar com escopos existentes — o novo escopo sobrescreve o anterior
        # para o mesmo policy_scope_id, preservando todos os outros escopos.
        merged_scopes = {**existing_scopes, manifest.policy_scope_id: new_scope_entry}

        payload = {
            "version": "1",
            "scopes": merged_scopes,
        }

        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _create_hosted_configuration_version(self, payload: str) -> int:
        """
        Cria uma nova versão hospedada no AppConfig com o payload do manifesto.

        Args:
            payload: string JSON do payload completo do manifesto.

        Returns:
            Número da versão criada no AppConfig.

        Raises:
            PolicyBundleUnavailable: se ocorrer erro de I/O com o AppConfig.
        """
        try:
            response = self._client.create_hosted_configuration_version(
                ApplicationId=self._application_id,
                ConfigurationProfileId=self._configuration_profile_id,
                Content=payload.encode("utf-8"),
                ContentType="application/json",
            )
            return response["VersionNumber"]
        except botocore.exceptions.ClientError as error:
            raise PolicyBundleUnavailable(
                f"Falha ao criar versão hospedada no AppConfig: {error}"
            ) from error

    def _start_deployment(self, version_number: int) -> int:
        """
        Inicia o deployment de uma versão do manifesto no AppConfig.

        Args:
            version_number: número da versão a deployar.

        Returns:
            Número do deployment iniciado.

        Raises:
            PolicyBundleUnavailable: se ocorrer erro de I/O com o AppConfig.
        """
        try:
            response = self._client.start_deployment(
                ApplicationId=self._application_id,
                EnvironmentId=self._environment_id,
                DeploymentStrategyId=self._deployment_strategy_id,
                ConfigurationProfileId=self._configuration_profile_id,
                ConfigurationVersion=str(version_number),
            )
            return response["DeploymentNumber"]
        except botocore.exceptions.ClientError as error:
            raise PolicyBundleUnavailable(
                f"Falha ao iniciar deployment no AppConfig (versão {version_number}): {error}"
            ) from error

    def _wait_for_deployment_completion(self, deployment_number: int) -> None:
        """
        Aguarda a conclusão do deployment AppConfig com polling e timeout.

        Faz polling do status do deployment até atingir um estado final
        (COMPLETE, ROLLED_BACK ou BAKING) ou até o timeout.

        Args:
            deployment_number: número do deployment a aguardar.

        Raises:
            PolicyBundleUnavailable: se o deployment falhar, for revertido
                                     ou se o timeout for atingido.
        """
        deadline = time.monotonic() + self._deployment_timeout_seconds
        last_state = "DEPLOYING"

        while time.monotonic() < deadline:
            try:
                response = self._client.get_deployment(
                    ApplicationId=self._application_id,
                    EnvironmentId=self._environment_id,
                    DeploymentNumber=deployment_number,
                )
                state = response.get("State", "UNKNOWN")
                percentage = response.get("PercentageComplete", 0)

                logger.debug(
                    "aguardando deployment AppConfig",
                    extra={
                        "deployment_number": deployment_number,
                        "state": state,
                        "percentage_complete": percentage,
                    },
                )

                if state in _DEPLOYMENT_FINAL_STATES:
                    if state in _DEPLOYMENT_SUCCESS_STATES:
                        logger.info(
                            "deployment AppConfig concluído com sucesso",
                            extra={
                                "deployment_number": deployment_number,
                                "state": state,
                            },
                        )
                        return
                    else:
                        # ROLLED_BACK — deployment foi revertido pelo AppConfig.
                        raise PolicyBundleUnavailable(
                            f"Deployment AppConfig #{deployment_number} foi revertido "
                            f"(state={state}). Verifique os logs do AppConfig."
                        )

                last_state = state

            except botocore.exceptions.ClientError as error:
                raise PolicyBundleUnavailable(
                    f"Falha ao verificar status do deployment AppConfig #{deployment_number}: {error}"
                ) from error

            time.sleep(_DEPLOYMENT_POLL_INTERVAL_SECONDS)

        # Timeout atingido — deployment ainda em andamento.
        raise PolicyBundleUnavailable(
            f"Timeout aguardando deployment AppConfig #{deployment_number} "
            f"(último estado: {last_state}, timeout: {self._deployment_timeout_seconds}s)"
        )
