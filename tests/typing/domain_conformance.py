"""Static assignments check implementations against domain-owned protocols."""
from pal.llm.contracts import LLMService, LLMProjectionPort
from pal.llm.runtime import LLMRuntime
from pal.execution.contracts import ExecutionRuntimePort
from pal.execution.runtime import ExecutionRuntime
from pal.memory.contracts import MemoryServicePort
from pal.memory.service import MemoryService
from pal.memory.history_contracts import HistoryRootPort, HistoryReadPort
from pal.memory.history_root import HistoryRoot
from pal.memory.turn_ir import L1TurnStore
from pal.llm.projection_contracts import ProjectionSessionPort
from pal.llm.projection_session import EndpointProjectionSession
from pal.shared.proactive_protocol import SilentOnceSchedulerPort
from pal.proactive.runtime import ProactiveManager


def proactive_conformance(manager: ProactiveManager) -> None:
    scheduler: SilentOnceSchedulerPort = manager

def execution_conformance(runtime: ExecutionRuntime) -> None:
    execution: ExecutionRuntimePort = runtime

def conformance(llm: LLMRuntime, memory: MemoryService, root: HistoryRoot, history: L1TurnStore, projection: EndpointProjectionSession) -> None:
    a: LLMService = llm
    b: LLMProjectionPort = llm
    c: MemoryServicePort = memory
    d: HistoryRootPort = root
    e: HistoryReadPort = history
    f: ProjectionSessionPort = projection


from pal.lsp.contracts import LspConnectorPort
from pal.lsp.connector import AsyncLspConnector
from pal.mcp.contracts import McpConnector
from pal.mcp.connector import AsyncStdioMcpConnector
from pal.execution.turn_io_contracts import TurnIOPort
from pal.core.runtime import CoreTurnIOPort


def connector_conformance(lsp: AsyncLspConnector, mcp: AsyncStdioMcpConnector, turn_io: CoreTurnIOPort) -> None:
    lsp_port: LspConnectorPort = lsp
    mcp_port: McpConnector = mcp
    turn_port: TurnIOPort = turn_io


from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.database import BunshinDatabase
from pal.bunshin.storage.transaction_session import TransactionSession


def bunshin_storage_conformance(database: BunshinDatabase, transaction: TransactionSession) -> None:
    standalone: DatabasePort = database
    bound: DatabasePort = transaction


from pal.llm.compaction_contracts import CompactionService
from pal.llm.compaction_engine import CompactionEngine


def compaction_conformance(engine: CompactionEngine) -> None:
    service: CompactionService = engine


from pal.control.contracts import ControlPlanePort
from pal.control.runtime_contracts import ControlRuntimePort, ControlRequestsPort, ContextChangePort
from pal.control.service import ControlPlane
from pal.core.control_runtime import CoreControlRuntime, ContextChange
from pal.core.control_requests import ControlRequests


def control_conformance(plane: ControlPlane, runtime: CoreControlRuntime,
                        requests: ControlRequests, change: ContextChange) -> None:
    control: ControlPlanePort = plane
    host: ControlRuntimePort = runtime
    pending: ControlRequestsPort = requests
    coordination: ContextChangePort = change


from pal.shared.timers import TimerHandlePort, TimerScopePort
from pal.core.timers import TimerHandle, TimerScope
from pal.llm.settings_contracts import RuntimeSettingsPort
from pal.llm.repository import RuntimeSettingRepository


def timer_conformance(handle: TimerHandle, scope: TimerScope, settings: RuntimeSettingRepository) -> None:
    timer: TimerHandlePort = handle
    owner: TimerScopePort = scope
    repository: RuntimeSettingsPort = settings


from pal.failure.contracts import FailureRecoveryPort
from pal.failure.recovery import FailureRecovery


def failure_conformance(recovery: FailureRecovery) -> None:
    service: FailureRecoveryPort = recovery


from pal.bunshin.event_contracts import BunshinEventPort
from pal.bunshin.capabilities import BunshinManagerProvider


def bunshin_event_conformance(manager: BunshinManagerProvider) -> None:
    events: BunshinEventPort = manager
