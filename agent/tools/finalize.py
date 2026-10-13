"""
WRITE_HIGH tool to finalize the questionnaire output.
Copies the validated filled spreadsheet to artifacts/<run_id>/final.xlsx.
Requires prior human sign-off (FINAL_SIGNOFF).
"""
import os
import shutil
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from agent.state import RiskLevel, RunState
from agent.tools.registry import Tool


class FinalizeOutputInput(BaseModel):
    source_file: str = Field(..., description="Path to filled xlsx")
    destination_file: Optional[str] = Field(None, description="Path for final signed-off artifact")


class FinalizeOutputOutput(BaseModel):
    final_path: str
    size_bytes: int


class FinalizeOutputTool(Tool):
    name = "finalize_output"
    description = "Copies validated questionnaire spreadsheet to artifacts final.xlsx. Requires WRITE_HIGH sign-off."
    risk = RiskLevel.WRITE_HIGH
    args_schema = FinalizeOutputInput
    output_schema = FinalizeOutputOutput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        source_file = kwargs["source_file"]
        dest = kwargs.get("destination_file")
        if not dest:
            dest = f"artifacts/{state.run_id}/final.xlsx"

        if not os.path.exists(source_file):
            raise FileNotFoundError(f"Source file not found: {source_file}")

        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        shutil.copyfile(source_file, dest)

        size = os.path.getsize(dest)
        state.artifacts["final_xlsx"] = dest
        return {
            "final_path": dest,
            "size_bytes": size,
        }

