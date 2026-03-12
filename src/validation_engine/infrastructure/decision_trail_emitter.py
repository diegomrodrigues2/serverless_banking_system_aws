"""
DecisionTrailEmitter — emissão best-effort do DecisionTrail ao Firehose.

Responsabilidade:
    Serializar e enviar o DecisionTrail ao Amazon Kinesis Data Firehose
    de forma assíncrona e best-effort. Falha na emissão NÃO invalida a
    transação aprovada — a corretude mínima é garantida pelo DecisionSummary
    persistido atomicamente com o JournalEntry.

Semântica best-effort (Requisito 13.3, 13.4):
    - Erros de serialização, rede ou Firehose são capturados e logados.
    - A exceção NÃO é propagada ao chamador.
    - O chamador (PolicyValidationFacade) continua normalmente após falha.

Formato do payload (Requisito 13.5, 21.3):
    - JSON UTF-8 com newline ao final (formato padrão Firehose para Parquet).
    - Campos planos para compatibilidade com Glue/Athena.
    - Particionamento por year/month/day/tenant_id/policy_scope_id é
      configurado no Firehose via prefixo dinâmico — não no payload.

Requisitos cobertos: 13.1, 13.3, 13.4, 13.5
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from validation_engine.domain.models import DecisionTrail

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol — contrato público do emitter
# ---------------------------------------------------------------------------


class DecisionTrailEmitter(Protocol):
    """
    Protocolo para emissão best-effort do DecisionTrail.

    Implementações devem garantir:
    - Falha de emissão NÃO propaga exceção ao chamador.
    - Falhas são logadas com contexto suficiente para diagnóstico.
    - O método retorna None em todos os casos (sucesso ou falha).

    Requisito: 13.3, 13.4
    """

    def emit(self, trail: "DecisionTrail") -> None:
        """
        Emite o DecisionTrail de forma best-effort.

        Args:
            trail: Trilha detalhada da avaliação a ser emitida.

        Returns:
            None — sempre, independentemente de sucesso ou falha.
        """
        ...


# ---------------------------------------------------------------------------
# Implementação Firehose
# ---------------------------------------------------------------------------


class FirehoseDecisionTrailEmitter:
    """
    Emitter que envia DecisionTrail ao Amazon Kinesis Data Firehose.

    Serializa o trail para JSON UTF-8 e envia ao stream Firehose configurado.
    Falhas são capturadas, logadas e silenciadas — nunca propagadas.

    O Firehose entrega os registros ao S3 em formato Parquet/Snappy,
    particionado por year/month/day/tenant_id/policy_scope_id conforme
    configurado no módulo Terraform infra/modules/firehose-decision-trail/.

    Uso:
        import boto3
        client = boto3.client("firehose", region_name="us-east-1")
        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=client,
            delivery_stream_name="validation-engine-decision-trail-dev",
        )
        emitter.emit(trail)

    Requisito: 13.1, 13.3, 13.4, 13.5
    """

    def __init__(
        self,
        firehose_client: object,
        delivery_stream_name: str,
    ) -> None:
        """
        Inicializa o emitter com o cliente Firehose e o nome do stream.

        Args:
            firehose_client:      Cliente boto3 Firehose já configurado.
            delivery_stream_name: Nome do Firehose Delivery Stream de destino.
        """
        self._firehose_client = firehose_client
        self._delivery_stream_name = delivery_stream_name

    def emit(self, trail: "DecisionTrail") -> None:
        """
        Serializa e envia o DecisionTrail ao Firehose de forma best-effort.

        Pipeline:
        1. Serializa o trail para JSON UTF-8 com newline ao final.
        2. Envia ao Firehose via PutRecord.
        3. Captura e loga qualquer exceção sem propagar.

        O newline ao final é necessário para que o Firehose separe
        corretamente os registros ao converter para Parquet.

        Args:
            trail: Trilha detalhada da avaliação a ser emitida.
        """
        try:
            payload = self._serialize_trail(trail)
            self._send_to_firehose(payload, trail)
        except Exception as exc:
            # Captura qualquer exceção — serialização, rede, Firehose, etc.
            # Falha de emissão NÃO invalida a transação (Requisito 13.4).
            self._log_emission_failure(trail, exc)

    def _serialize_trail(self, trail: "DecisionTrail") -> bytes:
        """
        Serializa o DecisionTrail para bytes JSON UTF-8 com newline.

        Usa to_firehose_payload() do trail para obter o dicionário plano
        e serializa para JSON com chaves ordenadas (determinístico).

        O newline ao final é o separador de registros esperado pelo Firehose
        para conversão correta para Parquet via Glue.

        Args:
            trail: Trilha a serializar.

        Returns:
            Bytes UTF-8 do JSON com newline ao final.

        Raises:
            Exception: qualquer erro de serialização (capturado pelo emit()).
        """
        payload_dict = trail.to_firehose_payload()
        # sort_keys=True garante serialização determinística para auditoria
        json_str = json.dumps(payload_dict, ensure_ascii=False, sort_keys=True)
        # Newline ao final é o separador de registros do Firehose
        return (json_str + "\n").encode("utf-8")

    def _send_to_firehose(self, payload: bytes, trail: "DecisionTrail") -> None:
        """
        Envia o payload serializado ao Firehose via PutRecord.

        Args:
            payload: Bytes UTF-8 do JSON serializado.
            trail:   Trail original (usado para logging em caso de erro).

        Raises:
            Exception: qualquer erro de rede ou Firehose (capturado pelo emit()).
        """
        self._firehose_client.put_record(
            DeliveryStreamName=self._delivery_stream_name,
            Record={"Data": payload},
        )

        logger.debug(
            "DecisionTrail emitido ao Firehose com sucesso",
            extra={
                "external_id": trail.external_id,
                "tenant_id": trail.tenant_id,
                "policy_scope_id": trail.policy_scope_id,
                "activation_id": trail.activation_id,
                "final_verdict": trail.final_verdict.value,
                "delivery_stream": self._delivery_stream_name,
            },
        )

    def _log_emission_failure(self, trail: "DecisionTrail", exc: Exception) -> None:
        """
        Loga a falha de emissão com contexto suficiente para diagnóstico.

        O log inclui os campos de identificação do trail para permitir
        correlação com o DecisionSummary persistido no ledger.

        Args:
            trail: Trail que falhou na emissão.
            exc:   Exceção capturada.
        """
        logger.error(
            "falha na emissão do DecisionTrail ao Firehose — transação não afetada",
            extra={
                "external_id": trail.external_id,
                "tenant_id": trail.tenant_id,
                "policy_scope_id": trail.policy_scope_id,
                "activation_id": trail.activation_id,
                "artifact_hash": trail.artifact_hash,
                "final_verdict": trail.final_verdict.value,
                "delivery_stream": self._delivery_stream_name,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Implementação no-op para testes e ambientes sem Firehose
# ---------------------------------------------------------------------------


class NoOpDecisionTrailEmitter:
    """
    Emitter no-op para testes unitários e ambientes sem Firehose configurado.

    Registra os trails emitidos em memória para inspeção nos testes.
    Nunca falha e nunca faz I/O.

    Uso em testes:
        emitter = NoOpDecisionTrailEmitter()
        facade = PolicyValidationFacade(..., trail_emitter=emitter)
        facade.validate(command)
        assert len(emitter.emitted_trails) == 1

    Requisito: 13.3, 13.4
    """

    def __init__(self) -> None:
        # Lista de trails emitidos — acessível nos testes para verificação
        self.emitted_trails: list["DecisionTrail"] = []

    def emit(self, trail: "DecisionTrail") -> None:
        """
        Registra o trail em memória sem I/O.

        Args:
            trail: Trilha a registrar.
        """
        self.emitted_trails.append(trail)


class FailingDecisionTrailEmitter:
    """
    Emitter que sempre falha — usado para testar isolamento de falha.

    Verifica que falha de emissão não propaga exceção ao chamador
    quando usado via FirehoseDecisionTrailEmitter (que captura erros).

    Uso em testes:
        # Testa que a facade não propaga falha do emitter
        emitter = FailingDecisionTrailEmitter()
        facade = PolicyValidationFacade(..., trail_emitter=emitter)
        # Não deve levantar exceção mesmo com emitter falhando
        result = facade.validate(command)

    Requisito: 13.4
    """

    def emit(self, trail: "DecisionTrail") -> None:
        """
        Sempre levanta RuntimeError para simular falha de emissão.

        Args:
            trail: Trail ignorado.

        Raises:
            RuntimeError: sempre, para simular falha.
        """
        raise RuntimeError("Falha simulada de emissão do DecisionTrail")
