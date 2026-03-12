"""
Testes de propriedade para a camada de infraestrutura do Double-Entry Ledger.

Propriedades cobertas:
- Property 17: Formato do posting_sort_key

Requisitos validados: 11.2
"""
import re

import pytest
from hypothesis import given, strategies as st

from ledger.infrastructure.dynamodb_mapper import build_posting_sort_key

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Gera entry_ids no formato UUID string
entry_id_strategy = st.uuids().map(str)

# Gera timestamps ISO 8601 no formato gerado pela factory
# Formato: YYYY-MM-DDTHH:MM:SS.ffffffZ
timestamp_strategy = st.datetimes(
    min_value=__import__("datetime").datetime(2020, 1, 1),
    max_value=__import__("datetime").datetime(2099, 12, 31),
).map(lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z")

# Gera indices ordinais validos (0-based, limite pratico de 97 postings por entry)
index_strategy = st.integers(min_value=0, max_value=97)

# Padrao regex para validar o formato do posting_sort_key
# Formato: POSTING#{timestamp}#{entry_id}#{index}
# Exemplo: POSTING#2026-03-10T14:30:00.000000Z#550e8400-e29b-41d4-a716-446655440000#0
_POSTING_SORT_KEY_PATTERN = re.compile(
    r"^POSTING#"                                    # prefixo obrigatorio
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z"  # timestamp ISO 8601
    r"#[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # UUID v4
    r"#\d+$"                                        # indice ordinal
)


# ---------------------------------------------------------------------------
# Property 17: Formato do posting_sort_key
# ---------------------------------------------------------------------------


# Feature: double-entry-ledger, Property 17: Formato do posting_sort_key
@pytest.mark.property
@given(
    timestamp=timestamp_strategy,
    entry_id=entry_id_strategy,
    index=index_strategy,
)
def test_posting_sort_key_format(timestamp: str, entry_id: str, index: int) -> None:
    """
    **Validates: Requirements 11.2**

    Para qualquer combinacao valida de (timestamp, entry_id, index),
    o posting_sort_key gerado deve:
    1. Comecar com o prefixo "POSTING#"
    2. Conter o timestamp ISO 8601 como segundo segmento
    3. Conter o entry_id (UUID v4) como terceiro segmento
    4. Conter o index ordinal como quarto segmento
    5. Ter exatamente 3 separadores "#" (4 segmentos no total)

    O formato canonico garante ordenacao cronologica lexicografica
    nas queries de extrato (Requisito 11.2).

    Invariante verificada:
        posting_sort_key == f"POSTING#{timestamp}#{entry_id}#{index}"
    """
    sort_key = build_posting_sort_key(timestamp, entry_id, index)

    # Invariante 1: comeca com prefixo POSTING#
    assert sort_key.startswith("POSTING#"), (
        f"posting_sort_key deve comecar com 'POSTING#'. Recebido: '{sort_key}'"
    )

    # Invariante 2: formato canonico exato
    expected = f"POSTING#{timestamp}#{entry_id}#{index}"
    assert sort_key == expected, (
        f"posting_sort_key deve ser '{expected}'. Recebido: '{sort_key}'"
    )

    # Invariante 3: exatamente 3 separadores '#' (4 segmentos)
    segments = sort_key.split("#")
    assert len(segments) == 4, (
        f"posting_sort_key deve ter 4 segmentos separados por '#'. "
        f"Recebido {len(segments)} segmentos: {segments}"
    )

    # Invariante 4: segmentos na ordem correta
    assert segments[0] == "POSTING", f"Primeiro segmento deve ser 'POSTING', recebido: '{segments[0]}'"
    assert segments[1] == timestamp, f"Segundo segmento deve ser o timestamp, recebido: '{segments[1]}'"
    assert segments[2] == entry_id, f"Terceiro segmento deve ser o entry_id, recebido: '{segments[2]}'"
    assert segments[3] == str(index), f"Quarto segmento deve ser o index, recebido: '{segments[3]}'"


@pytest.mark.property
@given(
    timestamp1=timestamp_strategy,
    timestamp2=timestamp_strategy,
    entry_id=entry_id_strategy,
    index=index_strategy,
)
def test_posting_sort_key_chronological_ordering(
    timestamp1: str,
    timestamp2: str,
    entry_id: str,
    index: int,
) -> None:
    """
    **Validates: Requirements 11.2**

    Para quaisquer dois timestamps t1 e t2 onde t1 < t2 (lexicograficamente),
    o posting_sort_key gerado com t1 deve ser menor que o gerado com t2.

    Esta propriedade garante que a ordenacao lexicografica do sort_key
    corresponde a ordenacao cronologica dos postings no extrato.

    Invariante verificada:
        t1 < t2 => sort_key(t1, entry_id, index) < sort_key(t2, entry_id, index)
    """
    sort_key1 = build_posting_sort_key(timestamp1, entry_id, index)
    sort_key2 = build_posting_sort_key(timestamp2, entry_id, index)

    # A ordenacao do sort_key deve refletir a ordenacao do timestamp
    if timestamp1 < timestamp2:
        assert sort_key1 < sort_key2, (
            f"sort_key com timestamp anterior deve ser menor. "
            f"t1='{timestamp1}' < t2='{timestamp2}' mas "
            f"sort_key1='{sort_key1}' >= sort_key2='{sort_key2}'"
        )
    elif timestamp1 > timestamp2:
        assert sort_key1 > sort_key2, (
            f"sort_key com timestamp posterior deve ser maior. "
            f"t1='{timestamp1}' > t2='{timestamp2}' mas "
            f"sort_key1='{sort_key1}' <= sort_key2='{sort_key2}'"
        )
    else:
        # Timestamps iguais -- sort_keys devem ser iguais (mesmo entry_id e index)
        assert sort_key1 == sort_key2
