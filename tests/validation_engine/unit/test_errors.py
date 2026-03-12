"""
Testes unitários para a hierarquia de erros do Validation Engine.

Verifica:
- Herança correta (cada erro é instância de ValidationEngineError e DomainError)
- Código de erro (code) correto para cada subclasse
- HTTP status correto para cada subclasse
- Mensagem padrão não-vazia para cada subclasse
- Override de mensagem customizada funciona corretamente
- Compatibilidade com str(e) via Exception base

Requisitos cobertos: 17.1, 17.3, 17.4, 17.5
"""
import pytest

from ledger.domain.errors import DomainError
from validation_engine.domain.errors import (
    InvalidPolicyBundle,
    PolicyBundleIntegrityFailure,
    PolicyBundleUnavailable,
    PolicyCostBudgetExceeded,
    PolicyEngineNotReady,
    PolicyEvaluationError,
    PolicyRejected,
    PolicySemanticError,
    PolicySnapshotUnavailable,
    PolicySyntaxError,
    ValidationEngineError,
)

# ---------------------------------------------------------------------------
# Mapeamento esperado: classe → (code, http_status)
# ---------------------------------------------------------------------------

# Cada entrada define o contrato esperado para a subclasse de erro.
# Usado para parametrizar testes de forma DRY.
EXPECTED_ERROR_CONTRACTS: list[tuple[type[ValidationEngineError], str, int]] = [
    (PolicySyntaxError, "POLICY_SYNTAX_ERROR", 400),
    (PolicySemanticError, "POLICY_SEMANTIC_ERROR", 400),
    (PolicyCostBudgetExceeded, "POLICY_COST_BUDGET_EXCEEDED", 400),
    (InvalidPolicyBundle, "INVALID_POLICY_BUNDLE", 400),
    (PolicyRejected, "POLICY_REJECTED", 422),
    (PolicyEngineNotReady, "POLICY_ENGINE_NOT_READY", 503),
    (PolicyBundleUnavailable, "POLICY_BUNDLE_UNAVAILABLE", 503),
    (PolicySnapshotUnavailable, "POLICY_SNAPSHOT_UNAVAILABLE", 503),
    (PolicyBundleIntegrityFailure, "POLICY_BUNDLE_INTEGRITY_FAILURE", 500),
    (PolicyEvaluationError, "POLICY_EVALUATION_ERROR", 500),
]

# Nomes legíveis para os IDs dos testes parametrizados
ERROR_IDS = [cls.__name__ for cls, _, _ in EXPECTED_ERROR_CONTRACTS]


# ---------------------------------------------------------------------------
# Testes de herança
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidationEngineErrorInheritance:
    """Verifica que a hierarquia de herança está correta."""

    def test_validation_engine_error_is_domain_error(self):
        """ValidationEngineError deve herdar de DomainError do ledger."""
        error = ValidationEngineError()
        assert isinstance(error, DomainError)

    def test_validation_engine_error_is_exception(self):
        """ValidationEngineError deve ser levantável como Exception."""
        with pytest.raises(ValidationEngineError):
            raise ValidationEngineError()

    @pytest.mark.parametrize(
        "error_class, _code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_subclass_is_instance_of_validation_engine_error(
        self, error_class, _code, _http_status
    ):
        """Cada subclasse deve ser instância de ValidationEngineError."""
        error = error_class()
        assert isinstance(error, ValidationEngineError)

    @pytest.mark.parametrize(
        "error_class, _code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_subclass_is_instance_of_domain_error(
        self, error_class, _code, _http_status
    ):
        """Cada subclasse deve ser instância de DomainError (herança transitiva)."""
        error = error_class()
        assert isinstance(error, DomainError)

    @pytest.mark.parametrize(
        "error_class, _code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_subclass_is_raiseable_as_validation_engine_error(
        self, error_class, _code, _http_status
    ):
        """Cada subclasse deve poder ser capturada como ValidationEngineError."""
        with pytest.raises(ValidationEngineError):
            raise error_class()


# ---------------------------------------------------------------------------
# Testes de código de erro (code)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidationEngineErrorCodes:
    """Verifica que cada subclasse tem o código de erro correto."""

    @pytest.mark.parametrize(
        "error_class, expected_code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_error_code_is_correct(self, error_class, expected_code, _http_status):
        """O campo code deve corresponder ao código esperado para cada subclasse."""
        error = error_class()
        assert error.code == expected_code

    @pytest.mark.parametrize(
        "error_class, expected_code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_error_code_is_string(self, error_class, expected_code, _http_status):
        """O campo code deve ser uma string."""
        error = error_class()
        assert isinstance(error.code, str)

    @pytest.mark.parametrize(
        "error_class, expected_code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_error_code_is_uppercase_snake_case(
        self, error_class, expected_code, _http_status
    ):
        """O código de erro deve estar em UPPER_SNAKE_CASE."""
        error = error_class()
        # Verifica que o código é uppercase e usa apenas letras, números e underscores
        assert error.code == error.code.upper()
        assert all(c.isalnum() or c == "_" for c in error.code)


# ---------------------------------------------------------------------------
# Testes de HTTP status
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidationEngineErrorHttpStatus:
    """Verifica que cada subclasse tem o HTTP status correto."""

    @pytest.mark.parametrize(
        "error_class, _code, expected_http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_http_status_is_correct(self, error_class, _code, expected_http_status):
        """O campo http_status deve corresponder ao status HTTP esperado."""
        error = error_class()
        assert error.http_status == expected_http_status

    @pytest.mark.parametrize(
        "error_class, _code, expected_http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_http_status_is_integer(self, error_class, _code, expected_http_status):
        """O campo http_status deve ser um inteiro."""
        error = error_class()
        assert isinstance(error.http_status, int)

    def test_400_errors_are_client_errors(self):
        """Erros de autoria/compilação devem ter status 4xx."""
        client_error_classes = [
            PolicySyntaxError,
            PolicySemanticError,
            PolicyCostBudgetExceeded,
            InvalidPolicyBundle,
        ]
        for error_class in client_error_classes:
            error = error_class()
            assert 400 <= error.http_status < 500, (
                f"{error_class.__name__} deve ter status 4xx, got {error.http_status}"
            )

    def test_500_errors_are_server_errors(self):
        """Erros internos do motor devem ter status 5xx."""
        server_error_classes = [
            PolicyBundleIntegrityFailure,
            PolicyEvaluationError,
            PolicyEngineNotReady,
            PolicyBundleUnavailable,
            PolicySnapshotUnavailable,
        ]
        for error_class in server_error_classes:
            error = error_class()
            assert 500 <= error.http_status < 600, (
                f"{error_class.__name__} deve ter status 5xx, got {error.http_status}"
            )


# ---------------------------------------------------------------------------
# Testes de mensagem
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidationEngineErrorMessages:
    """Verifica o comportamento das mensagens de erro."""

    @pytest.mark.parametrize(
        "error_class, _code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_default_message_is_non_empty(self, error_class, _code, _http_status):
        """A mensagem padrão de cada subclasse deve ser uma string não-vazia."""
        error = error_class()
        assert isinstance(error.message, str)
        assert len(error.message) > 0

    @pytest.mark.parametrize(
        "error_class, _code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_custom_message_override_works(self, error_class, _code, _http_status):
        """Deve ser possível sobrescrever a mensagem padrão com uma mensagem customizada."""
        custom_message = f"Mensagem customizada para {error_class.__name__}"
        error = error_class(message=custom_message)
        assert error.message == custom_message

    @pytest.mark.parametrize(
        "error_class, _code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_custom_message_does_not_change_code(
        self, error_class, _code, _http_status
    ):
        """Sobrescrever a mensagem não deve alterar o código de erro."""
        expected_code = _code
        error = error_class(message="Mensagem customizada")
        assert error.code == expected_code

    @pytest.mark.parametrize(
        "error_class, _code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_custom_message_does_not_change_http_status(
        self, error_class, _code, _http_status
    ):
        """Sobrescrever a mensagem não deve alterar o HTTP status."""
        expected_status = _http_status
        error = error_class(message="Mensagem customizada")
        assert error.http_status == expected_status

    @pytest.mark.parametrize(
        "error_class, _code, _http_status",
        EXPECTED_ERROR_CONTRACTS,
        ids=ERROR_IDS,
    )
    def test_str_representation_contains_message(
        self, error_class, _code, _http_status
    ):
        """str(error) deve conter a mensagem do erro (compatibilidade com Exception)."""
        error = error_class()
        assert error.message in str(error)
