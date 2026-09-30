"""Static assignments check implementations against domain-owned protocols."""
from pal.llm.contracts import LLMRuntimePort, LLMProjectionPort
from pal.llm.runtime import LLMRuntime
from pal.memory.contracts import MemoryServicePort
from pal.memory.service import MemoryService
from pal.memory.history_contracts import HistoryRootPort, HistoryReadPort
from pal.memory.history_root import HistoryRoot
from pal.memory.turn_ir import L1TurnStore
from pal.llm.projection_contracts import ProjectionSessionPort
from pal.llm.projection_session import EndpointProjectionSession

def conformance(llm: LLMRuntime, memory: MemoryService, root: HistoryRoot, history: L1TurnStore, projection: EndpointProjectionSession) -> None:
    a: LLMRuntimePort = llm
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


from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.database import BunshinDatabase
from pal.bunshin.v2.storage.transaction_session import TransactionSession


def bunshin_storage_conformance(database: BunshinDatabase, transaction: TransactionSession) -> None:
    standalone: DatabasePort = database
    bound: DatabasePort = transaction
