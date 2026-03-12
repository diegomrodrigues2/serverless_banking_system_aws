"""
Integração local: DecisionTrailEmitter com Firehose mockado via moto.

Testa:
- Emissão bem-sucedida de um DecisionTrail válido ao Firehose mockado
- Falha silenciosa: emitter não propaga exceção quando o Firehose falha
- Serialização correta do payload (JSON UTF-8 com newline)
- Campos obrigatórios presentes no payload enviado ao Firehose

Usa moto para simular o Firehose localmente sem dependências de AWS reais.

Semântica best-effort (Requisito 13.3, 13.4):
    Falha de emissão NÃO invalida a transação aprovada.
    O emitter captura e loga qualquer exceção sem propagar ao chamador.

Requisitos cobertos: 13.3, 13.4
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from validation_engine.domain.models import DecisionTrail, RuleMatchResult
from validation_engine.domain.policy_ast import FinalVerdict, PolicyEffect
from validation_engine.infrastructure.decision_trail_emitter import (
    FailingDecisionTrailEmitter,
    FirehoseDecisionTrailEmitter,
    NoOpDecisionTrailEmitter,
)

# ---------------------------------------------------------------------------
# Constantes de teste
# ---------------------------------------------------------------------------

_STREAM_NAME = "validation-engine-decision-trail-local-test"
_FAKE_KMS_KEY_ID = "arn:aws:kms:us-east-1:123456789012:key/test-key-id"


# ---------------------------------------------------------------------------
# Helpers de construção de DecisionTrail
# ---------------------------------------------------------------------------


def _make_trail(
    final_verdict: FinalVerdict = FinalVerdict.APPROVED,
    matched_deny_rule: str | None = None,
    error_code: str | None = None,
) -> DecisionTrail:
    """
    Constrói um DecisionTrail válido para testes de integração local.

    Inclui todos os campos obrigatórios conforme o schema do Firehose/Glue.
    """
    rules = (
        RuleMatchResult(
            rule_name="deny_over_limit",
            effect=PolicyEffect.DENY,
            matched=matched_deny_rule == "deny_over_limit",
            priority=100,
            message="Transaction exceeds daily limit",
        ),
        RuleMatchResult(
            rule_name="allow_standard",
            effect=PolicyEffect.ALLOW,
            matched=final_verdict == FinalVerdict.APPROVED,
            priority=10,
            message="Standard transaction",
        ),
    )

    return DecisionTrail(
        external_id="ext_local_emitter_test_001",
        tenant_id="tenantA",
        policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
        activation_id="act_local_emitter_001",
        artifact_hash="sha256:abc123def456",
        snapshot_version="snap_local_emitter_001",
        evaluator_version="1.0.0",
        input_hash="sha256:input_hash_local_001",
        final_verdict=final_verdict,
        matched_deny_rule=matched_deny_rule,
        rules=rules,
        evaluation_latency_ms=2.5,
        error_code=error_code,
        timestamp="2024-01-15T10:30:00Z",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def moto_firehose_client(moto_s3_client):
    """
    Cliente Firehose mockado via moto para testes locais.

    Reutiliza o contexto mock_aws já ativo via moto_s3_client para garantir
    que ambos os serviços compartilhem o mesmo mock de sessão.

    Yields:
        Cliente boto3 Firehose apontando para o mock moto.
    """
    import boto3

    client = boto3.client(
        "firehose",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    yield client


@pytest.fixture
def moto_firehose_stream(moto_firehose_client, moto_s3_client, local_s3_bucket):
    """
    Cria um Firehose delivery stream mockado via moto para testes locais.

    O stream usa o bucket S3 mockado como destino.

    Yields:
        Nome do stream criado.
    """
    # Cria o stream Firehose mockado apontando para o bucket S3 local
    moto_firehose_client.create_delivery_stream(
        DeliveryStreamName=_STREAM_NAME,
        DeliveryStreamType="DirectPut",
        ExtendedS3DestinationConfiguration={
            "RoleARN": "arn:aws:iam::123456789012:role/firehose-test-role",
            "BucketARN": f"arn:aws:s3:::{local_s3_bucket}",
            "Prefix": "trails/",
            "ErrorOutputPrefix": "errors/",
            "BufferingHints": {
                "SizeInMBs": 1,
                "IntervalInSeconds": 60,
            },
        },
    )

    yield _STREAM_NAME


@pytest.fixture
def firehose_emitter(moto_firehose_client, moto_firehose_stream):
    """
    FirehoseDecisionTrailEmitter configurado com o stream mockado via moto.

    Returns:
        FirehoseDecisionTrailEmitter pronto para uso em testes locais.
    """
    return FirehoseDecisionTrailEmitter(
        firehose_client=moto_firehose_client,
        delivery_stream_name=moto_firehose_stream,
    )


# ---------------------------------------------------------------------------
# Tests — Emissão bem-sucedida
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestLocalDecisionTrailEmitterSuccess:
    """
    Testa emissão bem-sucedida de DecisionTrail ao Firehose mockado.

    Requisitos: 13.3, 13.4
    """

    def test_emit_approved_trail_nao_levanta_excecao(
        self,
        firehose_emitter: FirehoseDecisionTrailEmitter,
    ) -> None:
        """
        Emissão de trail aprovado não deve levantar exceção.

        Verifica que o método emit() retorna None sem propagar erros.
        """
        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)

        # Não deve levantar exceção
        result = firehose_emitter.emit(trail)

        assert result is None

    def test_emit_rejected_trail_nao_levanta_excecao(
        self,
        firehose_emitter: FirehoseDecisionTrailEmitter,
    ) -> None:
        """
        Emissão de trail rejeitado não deve levantar exceção.

        Verifica que o método emit() retorna None mesmo para trails de rejeição.
        """
        trail = _make_trail(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_over_limit",
        )

        result = firehose_emitter.emit(trail)

        assert result is None

    def test_emit_envia_payload_json_correto_ao_firehose(
        self,
        moto_firehose_client,
        moto_firehose_stream: str,
    ) -> None:
        """
        O payload enviado ao Firehose deve ser JSON UTF-8 válido com os campos corretos.

        Usa um mock do put_record para capturar o payload enviado e verificar
        que contém todos os campos obrigatórios do DecisionTrail.
        """
        captured_payloads: list[bytes] = []

        # Intercepta a chamada put_record para capturar o payload
        original_put_record = moto_firehose_client.put_record

        def capture_put_record(**kwargs):
            captured_payloads.append(kwargs["Record"]["Data"])
            return original_put_record(**kwargs)

        moto_firehose_client.put_record = capture_put_record

        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=moto_firehose_client,
            delivery_stream_name=moto_firehose_stream,
        )

        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)
        emitter.emit(trail)

        assert len(captured_payloads) == 1

        # Verifica que o payload é JSON UTF-8 válido com newline ao final
        raw_payload = captured_payloads[0]
        assert raw_payload.endswith(b"\n"), "Payload deve terminar com newline"

        payload_dict = json.loads(raw_payload.decode("utf-8").strip())

        # Verifica campos obrigatórios do DecisionTrail
        assert payload_dict["external_id"] == trail.external_id
        assert payload_dict["tenant_id"] == trail.tenant_id
        assert payload_dict["policy_scope_id"] == trail.policy_scope_id
        assert payload_dict["activation_id"] == trail.activation_id
        assert payload_dict["artifact_hash"] == trail.artifact_hash
        assert payload_dict["snapshot_version"] == trail.snapshot_version
        assert payload_dict["evaluator_version"] == trail.evaluator_version
        assert payload_dict["input_hash"] == trail.input_hash
        assert payload_dict["final_verdict"] == "APPROVED"
        assert payload_dict["matched_deny_rule"] is None
        assert payload_dict["evaluation_latency_ms"] == trail.evaluation_latency_ms
        assert payload_dict["timestamp"] == trail.timestamp

    def test_emit_payload_contem_rules_serializadas(
        self,
        moto_firehose_client,
        moto_firehose_stream: str,
    ) -> None:
        """
        O payload deve conter a lista de rules avaliadas serializada.

        Verifica que o campo 'rules' contém os resultados de todas as rules.
        """
        captured_payloads: list[bytes] = []
        original_put_record = moto_firehose_client.put_record

        def capture_put_record(**kwargs):
            captured_payloads.append(kwargs["Record"]["Data"])
            return original_put_record(**kwargs)

        moto_firehose_client.put_record = capture_put_record

        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=moto_firehose_client,
            delivery_stream_name=moto_firehose_stream,
        )

        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)
        emitter.emit(trail)

        payload_dict = json.loads(captured_payloads[0].decode("utf-8").strip())

        # Verifica que rules é uma lista com os resultados corretos
        assert isinstance(payload_dict["rules"], list)
        assert len(payload_dict["rules"]) == 2

        rule_names = {r["rule_name"] for r in payload_dict["rules"]}
        assert "deny_over_limit" in rule_names
        assert "allow_standard" in rule_names

    def test_emit_payload_rejected_contem_matched_deny_rule(
        self,
        moto_firehose_client,
        moto_firehose_stream: str,
    ) -> None:
        """
        Payload de trail rejeitado deve conter o nome da rule DENY que rejeitou.
        """
        captured_payloads: list[bytes] = []
        original_put_record = moto_firehose_client.put_record

        def capture_put_record(**kwargs):
            captured_payloads.append(kwargs["Record"]["Data"])
            return original_put_record(**kwargs)

        moto_firehose_client.put_record = capture_put_record

        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=moto_firehose_client,
            delivery_stream_name=moto_firehose_stream,
        )

        trail = _make_trail(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_over_limit",
        )
        emitter.emit(trail)

        payload_dict = json.loads(captured_payloads[0].decode("utf-8").strip())

        assert payload_dict["final_verdict"] == "REJECTED"
        assert payload_dict["matched_deny_rule"] == "deny_over_limit"


# ---------------------------------------------------------------------------
# Tests — Falha silenciosa (best-effort semantics)
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestLocalDecisionTrailEmitterFailureSilence:
    """
    Testa que falha de emissão é silenciosa e não propaga exceção.

    Semântica best-effort: falha de emissão NÃO invalida a transação aprovada.
    O emitter captura e loga qualquer exceção sem propagar ao chamador.

    Requisito 13.4: falha de emissão não afeta a transação
    """

    def test_falha_do_firehose_nao_propaga_excecao(self) -> None:
        """
        Falha do Firehose (put_record levanta exceção) não deve propagar ao chamador.

        Verifica que o FirehoseDecisionTrailEmitter captura a exceção e retorna None.
        """
        mock_firehose_client = MagicMock()
        mock_firehose_client.put_record.side_effect = RuntimeError(
            "Firehose indisponível — simulação de falha"
        )

        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_firehose_client,
            delivery_stream_name=_STREAM_NAME,
        )

        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)

        # Não deve levantar exceção — falha é silenciosa
        result = emitter.emit(trail)

        assert result is None

    def test_falha_de_serializacao_nao_propaga_excecao(self) -> None:
        """
        Falha de serialização do trail não deve propagar ao chamador.

        Simula um trail com to_firehose_payload() que levanta exceção.
        """
        mock_firehose_client = MagicMock()

        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_firehose_client,
            delivery_stream_name=_STREAM_NAME,
        )

        # Cria um trail com to_firehose_payload() que levanta exceção
        mock_trail = MagicMock()
        mock_trail.to_firehose_payload.side_effect = ValueError(
            "Falha simulada de serialização"
        )
        mock_trail.external_id = "ext_fail_test"
        mock_trail.tenant_id = "tenantA"
        mock_trail.policy_scope_id = "tenantA:TRANSFER:*:*:prod"
        mock_trail.activation_id = "act_fail_test"
        mock_trail.artifact_hash = "sha256:fail"
        mock_trail.final_verdict = FinalVerdict.APPROVED

        # Não deve levantar exceção — falha é silenciosa
        result = emitter.emit(mock_trail)

        assert result is None

    def test_falha_de_rede_nao_propaga_excecao(self) -> None:
        """
        Falha de rede (ConnectionError) não deve propagar ao chamador.
        """
        mock_firehose_client = MagicMock()
        mock_firehose_client.put_record.side_effect = ConnectionError(
            "Falha de rede simulada"
        )

        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_firehose_client,
            delivery_stream_name=_STREAM_NAME,
        )

        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)

        # Não deve levantar exceção — falha de rede é silenciosa
        result = emitter.emit(trail)

        assert result is None

    def test_failing_emitter_nao_propaga_excecao(self) -> None:
        """
        FailingDecisionTrailEmitter sempre levanta RuntimeError internamente.

        Verifica que o FailingDecisionTrailEmitter propaga a exceção
        (comportamento esperado para testes de isolamento via facade).
        """
        emitter = FailingDecisionTrailEmitter()
        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)

        # FailingDecisionTrailEmitter PROPAGA a exceção — é usado para testar
        # que a facade captura a falha do emitter, não o emitter em si
        with pytest.raises(RuntimeError, match="Falha simulada"):
            emitter.emit(trail)

    def test_falha_loga_contexto_suficiente(self) -> None:
        """
        Falha de emissão deve ser logada com contexto suficiente para diagnóstico.

        Verifica que o logger.error é chamado com os campos de identificação
        do trail quando a emissão falha.
        """
        mock_firehose_client = MagicMock()
        mock_firehose_client.put_record.side_effect = RuntimeError("Firehose down")

        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_firehose_client,
            delivery_stream_name=_STREAM_NAME,
        )

        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)

        with patch(
            "validation_engine.infrastructure.decision_trail_emitter.logger"
        ) as mock_logger:
            emitter.emit(trail)

            # Verifica que logger.error foi chamado
            mock_logger.error.assert_called_once()

            # Verifica que o extra contém campos de identificação do trail
            call_kwargs = mock_logger.error.call_args
            extra = call_kwargs.kwargs.get("extra", {})

            assert extra.get("external_id") == trail.external_id
            assert extra.get("tenant_id") == trail.tenant_id
            assert extra.get("policy_scope_id") == trail.policy_scope_id
            assert extra.get("activation_id") == trail.activation_id


# ---------------------------------------------------------------------------
# Tests — NoOpDecisionTrailEmitter
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestNoOpDecisionTrailEmitter:
    """
    Testa o NoOpDecisionTrailEmitter para uso em testes e ambientes sem Firehose.

    Requisito 13.3, 13.4
    """

    def test_noop_emitter_registra_trail_em_memoria(self) -> None:
        """
        NoOpDecisionTrailEmitter deve registrar trails emitidos em memória.
        """
        emitter = NoOpDecisionTrailEmitter()
        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)

        emitter.emit(trail)

        assert len(emitter.emitted_trails) == 1
        assert emitter.emitted_trails[0] is trail

    def test_noop_emitter_registra_multiplos_trails(self) -> None:
        """
        NoOpDecisionTrailEmitter deve registrar múltiplos trails em ordem.
        """
        emitter = NoOpDecisionTrailEmitter()
        trail_approved = _make_trail(final_verdict=FinalVerdict.APPROVED)
        trail_rejected = _make_trail(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_over_limit",
        )

        emitter.emit(trail_approved)
        emitter.emit(trail_rejected)

        assert len(emitter.emitted_trails) == 2
        assert emitter.emitted_trails[0].final_verdict == FinalVerdict.APPROVED
        assert emitter.emitted_trails[1].final_verdict == FinalVerdict.REJECTED

    def test_noop_emitter_nao_levanta_excecao(self) -> None:
        """
        NoOpDecisionTrailEmitter nunca deve levantar exceção.
        """
        emitter = NoOpDecisionTrailEmitter()
        trail = _make_trail(final_verdict=FinalVerdict.APPROVED)

        result = emitter.emit(trail)

        assert result is None
