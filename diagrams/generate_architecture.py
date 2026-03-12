"""
Gerador de diagramas de arquitetura AWS do Double-Entry Ledger + Validation Engine.

Gera 5 diagramas complementares usando a biblioteca 'diagrams':
  1. Visão geral da arquitetura (todos os serviços e fluxos, incluindo Validation Engine)
  2. Write Path detalhado (fluxo síncrono de escrita com policy validation)
  3. Pipelines assíncronos (Outbox → EventBridge + Audit → Firehose → S3 + DecisionTrail)
  4. Validation Engine — Control Plane (autoria, compilação, storage, ativação)
  5. Validation Engine — Data Plane (avaliação determinística no hot path)

Execução:
    python diagrams/generate_architecture.py

Saída:
    diagrams/ledger_architecture_overview.png
    diagrams/ledger_write_path.png
    diagrams/ledger_async_pipelines.png
    diagrams/validation_engine_control_plane.png
    diagrams/validation_engine_data_plane.png
"""
from diagrams import Cluster, Diagram, Edge

from diagrams.aws.compute import Lambda
from diagrams.aws.database import DynamodbTable, DynamodbItems
from diagrams.aws.integration import Eventbridge, SQS
from diagrams.aws.network import APIGateway
from diagrams.aws.storage import S3
from diagrams.aws.analytics import KinesisDataFirehose, GlueCrawlers
from diagrams.aws.management import Cloudwatch, CloudwatchAlarm, SystemsManagerAppConfig
from diagrams.aws.security import KMS
from diagrams.onprem.client import Users


# ─── Cores para edges ────────────────────────────────────────────────────────
WRITE_COLOR = "#E74C3C"      # vermelho — write path
READ_COLOR = "#2ECC71"       # verde — read path
STREAM_COLOR = "#F39C12"     # laranja — DynamoDB Streams
EVENT_COLOR = "#3498DB"      # azul — EventBridge
AUDIT_COLOR = "#9B59B6"      # roxo — audit pipeline
DLQ_COLOR = "#95A5A6"        # cinza — DLQ / fallback
POLICY_COLOR = "#1ABC9C"     # teal — validation engine / policy
CONTROL_COLOR = "#E67E22"    # laranja escuro — control plane


# ═══════════════════════════════════════════════════════════════════════════════
# Diagrama 1 — Visão Geral da Arquitetura (com Validation Engine)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_overview():
    with Diagram(
        "Double-Entry Ledger + Validation Engine — Arquitetura AWS",
        filename="diagrams/ledger_architecture_overview",
        show=False,
        direction="LR",
        graph_attr={"fontsize": "14", "pad": "0.5", "nodesep": "0.8", "ranksep": "1.2"},
    ):
        clients = Users("API Clients")

        with Cluster("API Layer"):
            apigw = APIGateway("HTTP API\n(API Gateway v2)")
            cw_api = Cloudwatch("Access Logs")

        with Cluster("Compute — Lambda"):
            write_fn = Lambda("Write Lambda\nPOST /entries\nPOST /reversals")
            read_fn = Lambda("Read Lambda\nGET /balances\nGET /statements")
            publisher_fn = Lambda("Publisher Lambda\n(Outbox → EventBridge)")
            audit_fn = Lambda("Audit Transform\n(Stream → Firehose)")

        with Cluster("Validation Engine — Data Plane (in Write Lambda)"):
            policy_facade = Lambda("PolicyValidationFacade\n(ValidationChain)")
            appconfig_agent = SystemsManagerAppConfig("AppConfig Agent\n(Manifest)")

        with Cluster("Validation Engine — Control Plane"):
            s3_bundles = S3("S3 WORM\nBundles + Snapshots\n(Object Lock)")
            kms_ve = KMS("KMS\n(Envelope Encryption)")

        with Cluster("Validation Engine — Decision Trail"):
            firehose_trail = KinesisDataFirehose("Firehose\nDecisionTrail\n→ Parquet")
            s3_trail = S3("S3 Trail\nyear/month/day/\ntenant/scope")
            glue_trail = GlueCrawlers("Glue Catalog\n(Trail Schema)")

        with Cluster("Storage"):
            dynamo = DynamodbTable("DynamoDB\nSingle-Table\n(PITR + Streams)")
            streams = DynamodbItems("DynamoDB Streams\n(NEW_IMAGE)")

        with Cluster("Event Bus"):
            eventbridge = Eventbridge("EventBridge\nledger-events")

        with Cluster("Audit Pipeline"):
            firehose = KinesisDataFirehose("Kinesis Firehose\nJSON → Parquet")
            glue = GlueCrawlers("Glue Catalog\n(Audit Schema)")

        with Cluster("S3 Storage"):
            s3_audit = S3("S3 WORM\nAudit Bucket\n(Object Lock)")
            s3_errors = S3("S3 Error Bucket\n(Firehose Errors)")

        with Cluster("Dead Letter Queues"):
            publisher_dlq = SQS("Publisher DLQ")
            audit_dlq = SQS("Audit DLQ")

        with Cluster("Observabilidade — Validation Engine"):
            cw_alarms = CloudwatchAlarm("CloudWatch Alarms\n(Engine Not Ready,\nRefresh Failure,\nIntegrity, Rejections)")

        # ─── Fluxos ──────────────────────────────────────────────────────

        # API → Lambdas
        clients >> Edge(label="HTTPS", color=WRITE_COLOR) >> apigw
        apigw >> Edge(color=WRITE_COLOR) >> write_fn
        apigw >> Edge(color=READ_COLOR) >> read_fn
        apigw >> Edge(style="dashed", color=DLQ_COLOR) >> cw_api

        # Write Lambda → Validation Engine (policy evaluation no hot path)
        write_fn >> Edge(label="ValidationChain\n→ PolicyValidationFacade", color=POLICY_COLOR) >> policy_facade
        policy_facade >> Edge(label="ActivePolicySet\n(in-memory)", color=POLICY_COLOR, style="dashed") >> appconfig_agent
        appconfig_agent >> Edge(label="Manifest\nRefresh", color=POLICY_COLOR, style="dashed") >> s3_bundles

        # Validation Engine → DecisionTrail (best-effort)
        policy_facade >> Edge(label="DecisionTrail\n(best-effort)", color=POLICY_COLOR, style="dashed") >> firehose_trail
        firehose_trail >> Edge(label="Parquet\nDynamic Partitioning", color=POLICY_COLOR) >> s3_trail
        firehose_trail >> Edge(label="schema", style="dotted", color=POLICY_COLOR) >> glue_trail

        # S3 bundles encryption
        s3_bundles >> Edge(style="dotted", color=DLQ_COLOR) >> kms_ve

        # Write/Read → DynamoDB
        write_fn >> Edge(label="TransactWriteItems\n(+ DecisionSummary)", color=WRITE_COLOR) >> dynamo
        read_fn >> Edge(label="GetItem / Query", color=READ_COLOR) >> dynamo

        # DynamoDB → Streams → Lambdas
        dynamo >> Edge(color=STREAM_COLOR) >> streams
        streams >> Edge(label="filter: OUTBOX#", color=STREAM_COLOR) >> publisher_fn
        streams >> Edge(label="filter: JOURNAL#\nACCOUNT#", color=AUDIT_COLOR) >> audit_fn

        # Publisher → EventBridge + DLQ
        publisher_fn >> Edge(label="PutEvents", color=EVENT_COLOR) >> eventbridge
        publisher_fn >> Edge(label="fallback", style="dashed", color=DLQ_COLOR) >> publisher_dlq

        # Audit → Firehose → S3
        audit_fn >> Edge(label="PutRecordBatch", color=AUDIT_COLOR) >> firehose
        audit_fn >> Edge(label="fallback", style="dashed", color=DLQ_COLOR) >> audit_dlq
        firehose >> Edge(label="Parquet\nDynamic Partitioning", color=AUDIT_COLOR) >> s3_audit
        firehose >> Edge(label="errors", style="dashed", color=DLQ_COLOR) >> s3_errors
        firehose >> Edge(label="schema", style="dotted", color=AUDIT_COLOR) >> glue

        # Alarms
        policy_facade >> Edge(style="dotted", color=DLQ_COLOR) >> cw_alarms


# ═══════════════════════════════════════════════════════════════════════════════
# Diagrama 2 — Write Path Detalhado (com Validation Engine)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_write_path():
    with Diagram(
        "Double-Entry Ledger — Write Path (Consistência Forte + Policy Validation)",
        filename="diagrams/ledger_write_path",
        show=False,
        direction="LR",
        graph_attr={"fontsize": "14", "pad": "0.5", "nodesep": "0.7", "ranksep": "1.0"},
    ):
        client = Users("API Client")

        with Cluster("API Gateway"):
            apigw = APIGateway("HTTP API v2\nThrottling: 50 rps / 100 burst")

        with Cluster("Write Lambda"):
            write_fn = Lambda("Write Lambda\n(Python 3.11)")

        with Cluster("ValidationChain (em ordem)"):
            zsv = Lambda("ZeroSumValidator")
            muv = Lambda("MinorUnitsValidator")
            tlv = Lambda("TransactionLimitValidator")
            tiv = Lambda("TenantIsolationValidator")
            pvf = Lambda("PolicyValidationFacade\n(Validation Engine)")

        with Cluster("Validation Engine — Runtime (in-memory)"):
            ctx_builder = Lambda("CanonicalValidation\nContextBuilder")
            registry = Lambda("PolicyRuntimeRegistry\n(ActivePolicySet)")
            evaluator = Lambda("RuleEvaluator\n(função pura)")

        with Cluster("DynamoDB — TransactWriteItems (Atômica)"):
            journal = DynamodbTable("JournalEntry\nPK: JOURNAL#{id}\n(+ DecisionSummary)")
            postings = DynamodbTable("Postings (N)\nPK: ACCOUNT#{id}\nSK: POSTING#ts#id#idx")
            balances = DynamodbTable("Balance Updates (M)\nPK: ACCOUNT#{id}\nSK: BALANCE#{currency}\n(OCC: version check)")
            outbox = DynamodbTable("OutboxEvent\nPK: OUTBOX#{id}\n(TTL: expires_at)")
            idempotency = DynamodbTable("Idempotency\nPK: IDEMPOTENCY#{ext_id}\n(attribute_not_exists)")

        with Cluster("Observabilidade"):
            cw = Cloudwatch("CloudWatch Logs\n(Structured JSON)")

        # Fluxo principal
        client >> Edge(label="POST /entries\nPOST /reversals", color=WRITE_COLOR) >> apigw
        apigw >> Edge(color=WRITE_COLOR) >> write_fn

        # ValidationChain — validadores estruturais primeiro
        write_fn >> Edge(label="1. Structural\nValidation", color=WRITE_COLOR) >> zsv
        zsv >> Edge(color=WRITE_COLOR) >> muv
        muv >> Edge(color=WRITE_COLOR) >> tlv
        tlv >> Edge(color=WRITE_COLOR) >> tiv

        # ValidationChain — policy validation por último
        tiv >> Edge(label="2. Policy\nValidation", color=POLICY_COLOR) >> pvf

        # Validation Engine internals
        pvf >> Edge(label="build context", color=POLICY_COLOR) >> ctx_builder
        pvf >> Edge(label="get ActivePolicySet", color=POLICY_COLOR) >> registry
        pvf >> Edge(label="evaluate\n(zero I/O)", color=POLICY_COLOR) >> evaluator

        # Persistência atômica (com DecisionSummary)
        write_fn >> Edge(label="1 transação atômica\n(3 + N + M itens ≤ 100)\n+ DecisionSummary", color=WRITE_COLOR) >> journal
        write_fn >> Edge(color=WRITE_COLOR, style="dashed") >> postings
        write_fn >> Edge(color=WRITE_COLOR, style="dashed") >> balances
        write_fn >> Edge(color=WRITE_COLOR, style="dashed") >> outbox
        write_fn >> Edge(color=WRITE_COLOR, style="dashed") >> idempotency

        write_fn >> Edge(label="logs", style="dotted", color=DLQ_COLOR) >> cw


# ═══════════════════════════════════════════════════════════════════════════════
# Diagrama 3 — Pipelines Assíncronos (Event + Audit + DecisionTrail)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_async_pipelines():
    with Diagram(
        "Double-Entry Ledger — Pipelines Assíncronos (Event + Audit + DecisionTrail)",
        filename="diagrams/ledger_async_pipelines",
        show=False,
        direction="LR",
        graph_attr={"fontsize": "14", "pad": "0.5", "nodesep": "0.8", "ranksep": "1.0"},
    ):
        dynamo = DynamodbTable("DynamoDB\nSingle-Table")
        streams = DynamodbItems("DynamoDB Streams\n(NEW_IMAGE)")

        dynamo >> Edge(color=STREAM_COLOR) >> streams

        # ─── Event Pipeline (Outbox → EventBridge) ───────────────────────
        with Cluster("Event Pipeline (Transactional Outbox)"):
            publisher_fn = Lambda("Publisher Lambda\n(filter: OUTBOX#)")
            eventbridge = Eventbridge("EventBridge\nledger-events")
            publisher_dlq = SQS("Publisher DLQ\n(14 dias)")

        streams >> Edge(
            label="filter: PK starts_with OUTBOX#",
            color=STREAM_COLOR,
        ) >> publisher_fn

        publisher_fn >> Edge(
            label="PutEvents\nTransactionCreated\nTransactionReversed",
            color=EVENT_COLOR,
        ) >> eventbridge

        publisher_fn >> Edge(
            label="on failure",
            style="dashed",
            color=DLQ_COLOR,
        ) >> publisher_dlq

        # ─── Audit Pipeline (Stream → Firehose → S3 WORM) ───────────────
        with Cluster("Audit Pipeline (Compliance)"):
            audit_fn = Lambda("Audit Transform\n(filter: JOURNAL# + ACCOUNT#)\nbatch=100, window=30s")
            firehose = KinesisDataFirehose("Kinesis Firehose\nbuffer: 128MB / 60s\nJSON → Parquet (Snappy)")
            glue = GlueCrawlers("Glue Catalog\nDB + Table\n(AuditRecord schema)")
            audit_dlq = SQS("Audit DLQ\n(14 dias)")

        with Cluster("S3 — Armazenamento de Auditoria"):
            s3_audit = S3("S3 WORM Bucket\nObject Lock: GOVERNANCE\nLifecycle: IA→Glacier-IR\n\naudit/year=/month=/day=/tenant=/")
            s3_errors = S3("S3 Error Bucket\n(30 dias TTL)")

        streams >> Edge(
            label="filter: PK starts_with\nJOURNAL# | ACCOUNT#",
            color=AUDIT_COLOR,
        ) >> audit_fn

        audit_fn >> Edge(
            label="PutRecordBatch\n(AuditRecord JSON flat)",
            color=AUDIT_COLOR,
        ) >> firehose

        audit_fn >> Edge(
            label="on failure",
            style="dashed",
            color=DLQ_COLOR,
        ) >> audit_dlq

        firehose >> Edge(
            label="Parquet + Dynamic Partitioning\nyear/month/day/tenant",
            color=AUDIT_COLOR,
        ) >> s3_audit

        firehose >> Edge(
            label="conversion errors",
            style="dashed",
            color=DLQ_COLOR,
        ) >> s3_errors

        firehose >> Edge(
            label="schema lookup",
            style="dotted",
            color=AUDIT_COLOR,
        ) >> glue

        # ─── DecisionTrail Pipeline (Validation Engine → Firehose → S3) ──
        with Cluster("DecisionTrail Pipeline (Validation Engine)"):
            write_fn = Lambda("Write Lambda\n(PolicyValidationFacade)")
            firehose_trail = KinesisDataFirehose("Firehose DecisionTrail\nbuffer: 128MB / 60s\nJSON → Parquet (Snappy)")
            glue_trail = GlueCrawlers("Glue Catalog\n(DecisionTrail schema)")

        with Cluster("S3 — DecisionTrail Analytics"):
            s3_trail = S3("S3 Trail Bucket\nObject Lock: GOVERNANCE\nKMS Encryption\n\ntrail/year=/month=/day=/\ntenant=/scope=/")
            s3_trail_errors = S3("S3 Trail Error Bucket\n(30 dias TTL)")

        write_fn >> Edge(
            label="PutRecord\n(DecisionTrail JSON)\nbest-effort",
            color=POLICY_COLOR,
        ) >> firehose_trail

        firehose_trail >> Edge(
            label="Parquet + Dynamic Partitioning\nyear/month/day/tenant/scope",
            color=POLICY_COLOR,
        ) >> s3_trail

        firehose_trail >> Edge(
            label="conversion errors",
            style="dashed",
            color=DLQ_COLOR,
        ) >> s3_trail_errors

        firehose_trail >> Edge(
            label="schema lookup",
            style="dotted",
            color=POLICY_COLOR,
        ) >> glue_trail


# ═══════════════════════════════════════════════════════════════════════════════
# Diagrama 4 — Validation Engine: Control Plane
# ═══════════════════════════════════════════════════════════════════════════════

def generate_validation_control_plane():
    with Diagram(
        "Validation Engine — Control Plane (Autoria, Compilação, Ativação)",
        filename="diagrams/validation_engine_control_plane",
        show=False,
        direction="LR",
        graph_attr={"fontsize": "14", "pad": "0.5", "nodesep": "0.8", "ranksep": "1.2"},
    ):
        author = Users("Especialista de Domínio\n(Policy Author)")

        with Cluster("DSL Compilation Pipeline"):
            compiler = Lambda("DSLCompiler\n(Parser + AST)")
            semantic = Lambda("SemanticAnalyzer\n(tipos, escopos,\nnamespaces)")
            cost = Lambda("PolicyCostAnalyzer\n(limites estáticos:\nrules, depth,\naggregations)")
            pretty = Lambda("DSLPrettyPrinter\n(round-trip)")

        with Cluster("Golden Test Gate"):
            golden = Lambda("GoldenTestRunner\n(bundle + snapshot\n→ veredito esperado)")

        with Cluster("Artefact Storage (S3 WORM)"):
            s3_bundles = S3("S3 Bundles\nbundles/{artifact_hash}\nObject Lock + KMS")
            s3_snapshots = S3("S3 Snapshots\nsnapshots/{version}\nObject Lock + KMS")
            kms = KMS("KMS Key\n(Envelope Encryption)")

        with Cluster("Activation (AppConfig)"):
            publisher = Lambda("PolicyPublisher\n(gera Manifest,\nvalida compatibilidade)")
            appconfig = SystemsManagerAppConfig("AppConfig\nPolicyActivationManifest\n(por PolicyScope)")

        with Cluster("Observabilidade"):
            cw_alarms = CloudwatchAlarm("CloudWatch Alarms\n(Integrity Failure,\nActivation Errors)")

        # ─── Fluxo de compilação ─────────────────────────────────────────

        author >> Edge(label="Policy DSL\n(texto declarativo)", color=CONTROL_COLOR) >> compiler
        compiler >> Edge(label="RuleAST", color=CONTROL_COLOR) >> semantic
        semantic >> Edge(label="AST validado", color=CONTROL_COLOR) >> cost
        cost >> Edge(label="RuleBundle\n(artifact_hash)", color=CONTROL_COLOR) >> golden

        # Pretty printer (round-trip)
        compiler >> Edge(label="round-trip\nverification", style="dashed", color=DLQ_COLOR) >> pretty

        # ─── Armazenamento ───────────────────────────────────────────────

        golden >> Edge(label="store bundle\n(idempotente)", color=CONTROL_COLOR) >> s3_bundles
        author >> Edge(label="ReferenceSnapshot\n(dados auxiliares)", color=CONTROL_COLOR) >> s3_snapshots
        s3_bundles >> Edge(style="dotted", color=DLQ_COLOR) >> kms
        s3_snapshots >> Edge(style="dotted", color=DLQ_COLOR) >> kms

        # ─── Ativação ────────────────────────────────────────────────────

        golden >> Edge(
            label="após golden tests\npassarem",
            color=CONTROL_COLOR,
        ) >> publisher

        publisher >> Edge(
            label="PolicyActivationManifest\n(activation_id +\nartifact_hash +\nsnapshot_version +\ncontext_schema_version +\nevaluator_version)",
            color=CONTROL_COLOR,
        ) >> appconfig

        publisher >> Edge(style="dotted", color=DLQ_COLOR) >> cw_alarms


# ═══════════════════════════════════════════════════════════════════════════════
# Diagrama 5 — Validation Engine: Data Plane (Hot Path)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_validation_data_plane():
    with Diagram(
        "Validation Engine — Data Plane (Avaliação Determinística no Hot Path)",
        filename="diagrams/validation_engine_data_plane",
        show=False,
        direction="LR",
        graph_attr={"fontsize": "14", "pad": "0.5", "nodesep": "0.8", "ranksep": "1.2"},
    ):
        with Cluster("Ledger Bounded Context"):
            write_handler = Lambda("Write Handler\n(API)")
            ledger_engine = Lambda("LedgerEngine")
            chain = Lambda("ValidationChain")

            with Cluster("Structural Validators"):
                zsv = Lambda("ZeroSumValidator")
                muv = Lambda("MinorUnitsValidator")
                tlv = Lambda("TransactionLimitValidator")
                tiv = Lambda("TenantIsolationValidator")

            factory = Lambda("JournalEntryFactory\n(+ DecisionSummary)")
            repo = DynamodbTable("LedgerRepository\n(DynamoDB)")

        with Cluster("Validation Engine Bounded Context"):
            facade = Lambda("PolicyValidationFacade")

            with Cluster("Context Construction"):
                ctx_builder = Lambda("CanonicalValidation\nContextBuilder\n(DerivedFacts)")

            with Cluster("Policy Resolution (in-memory)"):
                registry = Lambda("PolicyRuntimeRegistry")
                aps = Lambda("ActivePolicySet\n(manifest + bundle\n+ snapshot)")
                lkg = Lambda("LKG Store\n(/tmp fallback)")

            with Cluster("Pure Evaluation"):
                evaluator = Lambda("RuleEvaluator\n(zero I/O,\nDENY_OVERRIDES)")

            with Cluster("Audit Output"):
                summary = Lambda("DecisionSummary\n(persistido\natomicamente)")
                trail_emitter = Lambda("DecisionTrailEmitter\n(best-effort)")

        with Cluster("External Dependencies (refresh only)"):
            appconfig = SystemsManagerAppConfig("AppConfig Agent\n(Manifest)")
            s3_bundles = S3("S3 Bundles")
            s3_snapshots = S3("S3 Snapshots")

        with Cluster("Trail Pipeline"):
            firehose = KinesisDataFirehose("Firehose\nDecisionTrail")

        with Cluster("Observabilidade"):
            cw_alarms = CloudwatchAlarm("CloudWatch Alarms\n(Not Ready, Refresh,\nIntegrity, Rejections,\nTrail Failures)")

        # ─── Fluxo do Ledger ─────────────────────────────────────────────

        write_handler >> Edge(label="command", color=WRITE_COLOR) >> ledger_engine
        ledger_engine >> Edge(label="validate", color=WRITE_COLOR) >> chain

        # Structural validators (em ordem)
        chain >> Edge(color=WRITE_COLOR) >> zsv
        zsv >> Edge(color=WRITE_COLOR) >> muv
        muv >> Edge(color=WRITE_COLOR) >> tlv
        tlv >> Edge(color=WRITE_COLOR) >> tiv

        # Policy validation (após structural)
        tiv >> Edge(label="policy\nvalidation", color=POLICY_COLOR) >> facade

        # ─── Fluxo interno do Validation Engine ──────────────────────────

        # 1. Build canonical context
        facade >> Edge(label="1. build\ncontext", color=POLICY_COLOR) >> ctx_builder

        # 2. Resolve active policy set
        facade >> Edge(label="2. get\nActivePolicySet", color=POLICY_COLOR) >> registry
        registry >> Edge(label="in-memory\ncache", color=POLICY_COLOR, style="dashed") >> aps
        registry >> Edge(label="fallback\n(após boot)", color=DLQ_COLOR, style="dashed") >> lkg

        # 3. Evaluate (pure function)
        facade >> Edge(label="3. evaluate\n(context + APS)", color=POLICY_COLOR) >> evaluator

        # 4. Build summary + emit trail
        facade >> Edge(label="4. DecisionSummary", color=POLICY_COLOR) >> summary
        facade >> Edge(label="5. DecisionTrail\n(best-effort)", color=POLICY_COLOR, style="dashed") >> trail_emitter

        # ─── Retorno ao Ledger ───────────────────────────────────────────

        facade >> Edge(
            label="ValidationResult\n(+ artifacts)",
            color=POLICY_COLOR,
            style="bold",
        ) >> chain

        ledger_engine >> Edge(label="create entry\n(+ summary)", color=WRITE_COLOR) >> factory
        factory >> Edge(label="TransactWriteItems", color=WRITE_COLOR) >> repo

        # ─── Refresh (fora do hot path) ──────────────────────────────────

        registry >> Edge(
            label="refresh\n(swap atômico)",
            color=CONTROL_COLOR,
            style="dotted",
        ) >> appconfig

        appconfig >> Edge(
            label="manifest\nresolution",
            color=CONTROL_COLOR,
            style="dotted",
        ) >> s3_bundles

        appconfig >> Edge(
            color=CONTROL_COLOR,
            style="dotted",
        ) >> s3_snapshots

        # Trail → Firehose
        trail_emitter >> Edge(label="PutRecord", color=POLICY_COLOR, style="dashed") >> firehose

        # Alarms
        registry >> Edge(style="dotted", color=DLQ_COLOR) >> cw_alarms
        trail_emitter >> Edge(style="dotted", color=DLQ_COLOR) >> cw_alarms


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("Gerando diagrama 1/5: Visão Geral da Arquitetura...")
    generate_overview()

    print("Gerando diagrama 2/5: Write Path Detalhado...")
    generate_write_path()

    print("Gerando diagrama 3/5: Pipelines Assíncronos...")
    generate_async_pipelines()

    print("Gerando diagrama 4/5: Validation Engine — Control Plane...")
    generate_validation_control_plane()

    print("Gerando diagrama 5/5: Validation Engine — Data Plane...")
    generate_validation_data_plane()

    print("\nDiagramas gerados com sucesso:")
    print("  - diagrams/ledger_architecture_overview.png")
    print("  - diagrams/ledger_write_path.png")
    print("  - diagrams/ledger_async_pipelines.png")
    print("  - diagrams/validation_engine_control_plane.png")
    print("  - diagrams/validation_engine_data_plane.png")
