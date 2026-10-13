"""
Base tool interfaces and registry with risk enforcement.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel
from agent.state import RiskLevel, RunState


class ToolExecutionError(Exception):
    """Raised when tool execution fails."""
    pass


class ApprovalRequiredError(PermissionError):
    """Raised when a WRITE_HIGH tool is invoked without prior approval."""
    pass


class Tool(ABC):
    name: str
    description: str
    risk: RiskLevel = RiskLevel.READ
    args_schema: Type[BaseModel]
    output_schema: Optional[Type[BaseModel]] = None

    @abstractmethod
    def run(self, state: RunState, **kwargs: Any) -> Any:
        """Executes the tool given the current state and arguments."""
        pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name} is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not found in registry.")
        return self._tools[name]

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def export_for_llm(self) -> List[Dict[str, Any]]:
        """Exports tools formatted as JSON-schema tool specs for LLM tool calling."""
        specs: List[Dict[str, Any]] = []
        for tool in self._tools.values():
            specs.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": f"[{tool.risk.value}] {tool.description}",
                    "parameters": tool.args_schema.model_json_schema() if tool.args_schema else {},
                }
            })
        return specs

    def execute(self, tool_name: str, state: RunState, **kwargs: Any) -> Any:
        """
        Executes a tool after validating inputs and enforcing risk policy:
        WRITE_HIGH tools require state.pending_approval to be present, matching, and approved.
        """
        tool = self.get(tool_name)

        # Validate arguments using args_schema
        if tool.args_schema:
            validated_args = tool.args_schema(**kwargs)
            call_kwargs = validated_args.model_dump()
        else:
            call_kwargs = kwargs

        # Enforce WRITE_HIGH security guarantee
        if tool.risk == RiskLevel.WRITE_HIGH:
            approval = state.pending_approval
            if not approval:
                raise ApprovalRequiredError(
                    f"Refusing to execute WRITE_HIGH tool '{tool.name}': no pending approval exists."
                )
            if not approval.approved:
                raise ApprovalRequiredError(
                    f"Refusing to execute WRITE_HIGH tool '{tool.name}': approval request '{approval.id}' has not been approved."
                )

        # Run tool
        result = tool.run(state, **call_kwargs)

        # Optional output validation
        if tool.output_schema and isinstance(result, dict):
            return tool.output_schema(**result)
        return result

