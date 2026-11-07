from tenxgraph import StateGraph
from tenxgraph.core.state import AgentState
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from tenxgraph.utils.constants import END


def hello(state: AgentState):
    return state


graph = StateGraph()
graph.add_node("HELLO", hello)
graph.add_edge("HELLO", END)
graph.set_entry_point("HELLO")

app = graph.compile(checkpointer=InMemoryCheckpointer())
