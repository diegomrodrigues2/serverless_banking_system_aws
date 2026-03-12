"""
Testes de integração AWS dev — DecisionTrailEmitter e Firehose real.

Emite um DecisionTrail real ao Firehose stream em AWS dev e verifica:
- O registro chega ao bucket S3 de destino (com retry/wait logic)
- A chave S3 segue a estrutura de particionamento esperada:
  trails/year=YYYY/month=MM/day=DD/tenant_id=X/policy_scope_id=Y/

Usa recursos AWS REAIS (Firehose, S3). NÃO usa moto ou qualquer mock.

Pré-requisitos:
    - VALIDATION_ENGINE_TEST_BUCKET: bucket S3 de destino dos trails
    - VALIDATION_ENGINE_TEST_FIREHOSE_STREAM: nome do Firehose stream
    - AWS_REGION: região AWS (padrão: us-east-1)
    - Credenciais AWS válidas com permissão de emissão ao Firehose e leitura do S3

Estratégia de isolamento:
    O run_id (UUID único por sessão) é embutido no external_id e tenant_id
    para garantir que trails de sessões diferentes não colidam entre si.

Nota sobre latência do Firehose:
    O Firehose tem latência de entrega ao S3 (buffer de 60s a 900s).
    Os testes usam retry/wait logic com timeout configurável para aguardar
    a chegada dos registros ao S3.

Requisitos cobertos: 13.5, 21.1, 21.4
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import pytest

from validation_engine.domain.models import DecisionTrail, RuleMatchResult
from validation_engine.domain.policy_ast import FinalVerdict, PolicyEffect
from validation_engine.infrastructure.decision_trail_emitter import (
    FirehoseDecisionTrailEmitter,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de configuração
# ---------------------------------------------------------------------------

FIREHOSE_STREAM_ENV_VAR = "VALIDATION_ENGINE_TEST_FIREHOSE_STREAM"
TRAIL_BUCKET_ENV_VAR = "VALIDATION_ENGINE_TEST_TRAIL_BUCKET"

# Timeout máximo para aguardar chegada do trail ao S3 (segundos)
# O Firehose tem buffer de 60s por padrão — aguardamos até 5 minutos
S3_DELIVERY_TIMEOUT_SECONDS = 300

# Intervalo entre tentativas de verificação no S3
S3_POLL_INTERVAL_SECONDS = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_firehose_stream_name() -> str:
    """
    Obtém o nome do Firehose stream de teste da variável de ambiente.

    Ignora o teste se a variável não estiver definida.
    """
    stream_name = os.environ.get(FIREHOSE_STREAM_ENV_VAR, "")
    if not stream_name:
        pytest.skip(
            f"{FIREHOSE_STREAM_ENV_VAR} não definido — "
            "testes de integração Firehose AWS dev ignorados"
        )
    return stream_name


def _get_trail_bucket_name(aws_dev_config) -> str:
    """
    Obtém o nome do bucket S3 de destino dos trails.

    Usa VALIDATION_ENGINE_TEST_TRAIL_BUCKET se definido,
    caso contrário usa o bucket padrão de testes.
    """
    return os.environ.get(TRAIL_BUCKET_ENV_VAR, "") or aws_dev_config.bucket


def _make_test_trail(run_id: str, timestamp: str) -> DecisionTrail:
    """
    Constrói um DecisionTrail de teste com run_id único para isolamento.

    O run_id é embutido no external_id e tenant_id para garantir que
    trails de sessões diferentes não colidam na verificação S3.

    Args:
        run_id:    UUID único da sessão de testes.
        timestamp: Timestamp ISO 8601 da avaliação (usado para particionamento).

    Returns:
        DecisionTrail com identificadores únicos para esta sessão.
    """
    # Usa um tenant_id curto derivado do run_id para o particionamento S3
    # O policy_scope_id também usa o run_id para isolamento
    short_run_id = run_id.replace("-", "")[:12]
    tenant_id = f"test-{short_run_id}"
    policy_scope_id = f"{tenant_id}:TRANSFER:PIX:*:dev"

    rules = (
        RuleMatchResult(
            rule_name="deny_over_limit",
            effect=PolicyEffect.DENY,
            matched=False,
            priority=100,
            message="Transaction exceeds daily limit",
        ),
        RuleMatchResult(
            rule_name="allow_standard",
            effect=PolicyEffect.ALLOW,
            matched=True,
            priority=10,
            message="Standard transaction",
        ),
    )

    return DecisionTrail(
        external_id=f"ext-aws-firehose-test-{run_id}",
        tenant_id=tenant_id,
        policy_scope_id=policy_scope_id,
        activation_id=f"act-aws-firehose-test-{run_id}",
        artifact_hash="sha256:aws_firehose_test_bundle_hash",
        snapshot_version=f"snap-aws-firehose-test-{run_id}",
        evaluator_version="1.0.0",
        input_hash="sha256:aws_firehose_test_input_hash",
        final_verdict=FinalVerdict.APPROVED,
        matched_deny_rule=None,
        rules=rules,
        evaluation_latency_ms=3.7,
        error_code=None,
        timestamp=timestamp,
    )


def _wait_for_s3_object(
    s3_client,
    bucket: str,
    prefix: str,
    timeout_seconds: int = S3_DELIVERY_TIMEOUT_SECONDS,
    poll_interval_seconds: int = S3_POLL_INTERVAL_SECONDS,
) -> list[str]:
    """
    Aguarda a chegada de objetos S3 sob um prefixo com retry/wait logic.

    Faz polling no S3 até encontrar objetos sob o prefixo ou atingir o timeout.

    Args:
        s3_client:             Cliente boto3 S3.
        bucket:                Nome do bucket S3.
        prefix:                Prefixo S3 a verificar.
        timeout_seconds:       Timeout máximo em segundos.
        poll_interval_seconds: Intervalo entre tentativas em segundos.

    Returns:
        Lista de chaves S3 encontradas sob o prefixo.

    Raises:
        TimeoutError: Se nenhum objeto for encontrado dentro do timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        logger.info(
            "Verificando chegada de trail no S3",
            extra={
                "bucket": bucket,
                "prefix": prefix,
                "attempt": attempt,
                "remaining_seconds": int(deadline - time.monotonic()),
            },
        )

        response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        objects = response.get("Contents", [])

        if objects:
            keys = [obj["Key"] for obj in objects]
            logger.info(
                "Trail encontrado no S3",
                extra={"bucket": bucket, "keys": keys, "attempt": attempt},
            )
            return keys

        # Aguarda antes da próxima tentativa
        time.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"Trail não encontrado no S3 após {timeout_seconds}s. "
        f"Bucket: {bucket}, Prefix: {prefix}"
    )


# ---------------------------------------------------------------------------
# Fixtures de módulo (escopo module para reutilizar recursos AWS)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def firehose_stream_name() -> str:
    """Nome do Firehose stream de teste — ignora se não configurado."""
    return _get_firehose_stream_name()


@pytest.fixture(scope="module")
def trail_bucket_name(aws_dev_config) -> str:
    """Nome do bucket S3 de destino dos trails."""
    return _get_trail_bucket_name(aws_dev_config)


@pytest.fixture(scope="module")
def aws_firehose_client(aws_dev_config):
    """
    Cliente Firehose apontando para AWS real em ambiente dev.

    Returns:
        Cliente boto3 Firehose configurado para a região do ambiente dev.
    """
    import boto3

    return boto3.client("firehose", region_name=aws_dev_config.region)


@pytest.fixture(scope="module")
def firehose_emitter(aws_firehose_client, firehose_stream_name) -> FirehoseDecisionTrailEmitter:
    """
    FirehoseDecisionTrailEmitter configurado com o stream real em AWS dev.

    Returns:
        FirehoseDecisionTrailEmitter pronto para emitir trails reais.
    """
    return FirehoseDecisionTrailEmitter(
        firehose_client=aws_firehose_client,
        delivery_stream_name=firehose_stream_name,
    )


@pytest.fixture(scope="module")
def test_trail(aws_dev_config) -> DecisionTrail:
    """
    DecisionTrail de teste com run_id único para isolamento.

    O timestamp é gerado no momento da criação da fixture para garantir
    que o particionamento S3 reflita o momento real do teste.

    Returns:
        DecisionTrail com identificadores únicos para esta sessão.
    """
    # Timestamp atual em UTC para particionamento correto no S3
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    return _make_test_trail(
        run_id=aws_dev_config.run_id,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSFirehoseTrailEmission:
    """
    Testa emissão real de DecisionTrail ao Firehose em AWS dev.

    Requisitos: 13.5, 21.1
    """

    def test_emit_trail_real_nao_levanta_excecao(
        self,
        firehose_emitter: FirehoseDecisionTrailEmitter,
        test_trail: DecisionTrail,
    ) -> None:
        """
        Emissão de trail real ao Firehose não deve levantar exceção.

        Verifica que o método emit() retorna None sem propagar erros
        ao emitir para o Firehose real em AWS dev.
        """
        result = firehose_emitter.emit(test_trail)

        assert result is None

    def test_emit_trail_real_com_verdict_aprovado(
        self,
        firehose_emitter: FirehoseDecisionTrailEmitter,
        aws_dev_config,
    ) -> None:
        """
        Emissão de trail aprovado ao Firehose real deve completar sem erros.

        Emite um trail com FinalVerdict.APPROVED e verifica que não há exceção.
        """
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        trail = _make_test_trail(
            run_id=f"{aws_dev_config.run_id}-approved",
            timestamp=timestamp,
        )

        result = firehose_emitter.emit(trail)

        assert result is None

    def test_emit_trail_real_com_verdict_rejeitado(
        self,
        firehose_emitter: FirehoseDecisionTrailEmitter,
        aws_dev_config,
    ) -> None:
        """
        Emissão de trail rejeitado ao Firehose real deve completar sem erros.

        Emite um trail com FinalVerdict.REJECTED e verifica que não há exceção.
        """
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        short_run_id = aws_dev_config.run_id.replace("-", "")[:12]
        tenant_id = f"test-{short_run_id}"
        policy_scope_id = f"{tenant_id}:TRANSFER:PIX:*:dev"

        trail = DecisionTrail(
            external_id=f"ext-aws-firehose-rejected-{aws_dev_config.run_id}",
            tenant_id=tenant_id,
            policy_scope_id=policy_scope_id,
            activation_id=f"act-aws-firehose-rejected-{aws_dev_config.run_id}",
            artifact_hash="sha256:aws_firehose_test_bundle_hash",
            snapshot_version=f"snap-aws-firehose-rejected-{aws_dev_config.run_id}",
            evaluator_version="1.0.0",
            input_hash="sha256:aws_firehose_test_input_hash_rejected",
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_over_limit",
            rules=(
                RuleMatchResult(
                    rule_name="deny_over_limit",
                    effect=PolicyEffect.DENY,
                    matched=True,
                    priority=100,
                    message="Transaction exceeds daily limit",
                ),
            ),
            evaluation_latency_ms=1.2,
            error_code=None,
            timestamp=timestamp,
        )

        result = firehose_emitter.emit(trail)

        assert result is None


@pytest.mark.integration_aws_dev
@pytest.mark.slow
class TestAWSFirehoseS3Delivery:
    """
    Testa a chegada do DecisionTrail ao S3 após emissão ao Firehose real.

    Estes testes são marcados como @pytest.mark.slow porque aguardam
    a entrega do Firehose ao S3 (latência de 60s a 5 minutos).

    Execute com: pytest -m "integration_aws_dev and slow" -v

    Requisitos: 13.5, 21.1, 21.4
    """

    def test_trail_chega_ao_s3_apos_emissao(
        self,
        firehose_emitter: FirehoseDecisionTrailEmitter,
        aws_dev_s3_client,
        trail_bucket_name: str,
        test_trail: DecisionTrail,
    ) -> None:
        """
        Trail emitido ao Firehose deve chegar ao bucket S3 de destino.

        Emite o trail e aguarda a chegada ao S3 com retry/wait logic.
        O Firehose tem latência de entrega — este teste pode levar até 5 minutos.

        Requisito 21.1: pipeline assíncrono para armazenamento analítico
        """
        # Emite o trail ao Firehose real
        firehose_emitter.emit(test_trail)

        logger.info(
            "Trail emitido ao Firehose — aguardando entrega ao S3",
            extra={
                "stream": firehose_emitter._delivery_stream_name,
                "bucket": trail_bucket_name,
                "external_id": test_trail.external_id,
            },
        )

        # Aguarda a chegada ao S3 com retry/wait logic
        # O prefixo base é "trails/" — o particionamento completo é verificado
        # no teste de estrutura de partição abaixo
        try:
            found_keys = _wait_for_s3_object(
                s3_client=aws_dev_s3_client,
                bucket=trail_bucket_name,
                prefix="trails/",
                timeout_seconds=S3_DELIVERY_TIMEOUT_SECONDS,
                poll_interval_seconds=S3_POLL_INTERVAL_SECONDS,
            )
            assert len(found_keys) >= 1, "Pelo menos um objeto deve existir no S3"
        except TimeoutError as exc:
            pytest.fail(
                f"Trail não chegou ao S3 dentro do timeout: {exc}\n"
                f"Verifique se o Firehose stream '{firehose_emitter._delivery_stream_name}' "
                f"está configurado corretamente e apontando para o bucket '{trail_bucket_name}'."
            )

    def test_chave_s3_segue_estrutura_de_particao_esperada(
        self,
        firehose_emitter: FirehoseDecisionTrailEmitter,
        aws_dev_s3_client,
        trail_bucket_name: str,
        test_trail: DecisionTrail,
        aws_dev_config,
    ) -> None:
        """
        A chave S3 do trail deve seguir a estrutura de particionamento esperada.

        Estrutura esperada:
            trails/year=YYYY/month=MM/day=DD/tenant_id=X/policy_scope_id=Y/

        Verifica que o particionamento dinâmico do Firehose está funcionando
        corretamente com os campos tenant_id e policy_scope_id do DecisionTrail.

        Requisito 21.4: particionamento por year/month/day/tenant_id/policy_scope_id
        """
        # Emite o trail ao Firehose real
        firehose_emitter.emit(test_trail)

        # Extrai os componentes de particionamento esperados do timestamp do trail
        # Formato do timestamp: "2024-01-15T10:30:00Z"
        ts = test_trail.timestamp
        expected_year = ts[0:4]
        expected_month = ts[5:7]
        expected_day = ts[8:10]
        expected_tenant_id = test_trail.tenant_id
        expected_policy_scope_id = test_trail.policy_scope_id

        # Prefixo esperado com particionamento completo
        expected_prefix = (
            f"trails/"
            f"year={expected_year}/"
            f"month={expected_month}/"
            f"day={expected_day}/"
            f"tenant_id={expected_tenant_id}/"
            f"policy_scope_id={expected_policy_scope_id}/"
        )

        logger.info(
            "Verificando estrutura de partição S3",
            extra={
                "expected_prefix": expected_prefix,
                "bucket": trail_bucket_name,
            },
        )

        try:
            found_keys = _wait_for_s3_object(
                s3_client=aws_dev_s3_client,
                bucket=trail_bucket_name,
                prefix=expected_prefix,
                timeout_seconds=S3_DELIVERY_TIMEOUT_SECONDS,
                poll_interval_seconds=S3_POLL_INTERVAL_SECONDS,
            )

            # Verifica que pelo menos uma chave segue a estrutura esperada
            assert len(found_keys) >= 1, (
                f"Nenhum objeto encontrado com o prefixo de partição esperado: {expected_prefix}"
            )

            # Verifica que todas as chaves encontradas seguem a estrutura esperada
            for key in found_keys:
                assert key.startswith(expected_prefix), (
                    f"Chave S3 '{key}' não segue a estrutura de partição esperada. "
                    f"Prefixo esperado: '{expected_prefix}'"
                )

            logger.info(
                "Estrutura de partição S3 verificada com sucesso",
                extra={
                    "found_keys": found_keys,
                    "expected_prefix": expected_prefix,
                },
            )

        except TimeoutError as exc:
            pytest.fail(
                f"Trail não chegou ao S3 com a estrutura de partição esperada: {exc}\n"
                f"Prefixo esperado: {expected_prefix}\n"
                f"Verifique se o Dynamic Partitioning do Firehose está configurado "
                f"para extrair year, month, day, tenant_id e policy_scope_id do payload."
            )
