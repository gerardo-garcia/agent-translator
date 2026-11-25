from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class LookupTicketInput(BaseModel):
    """Input schema for LookupTicket."""
    ticket_id: str = Field(..., description="Ticket identifier.")


class LookupTicket(BaseTool):
    name: str = "lookup_ticket"
    description: str = "Look up a support ticket by id and return its history."
    args_schema: type[BaseModel] = LookupTicketInput

    def _run(self, ticket_id: str) -> str:
        return f"Ticket {ticket_id}: no history (example tool)."
