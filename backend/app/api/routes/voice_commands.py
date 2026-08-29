"""Local voice-command intake.

This route deliberately starts as validation-only.  A speech recogniser is an
untrusted client: it may never choose a non-existent printer, invent a letter
file, or bypass Bambuddy's normal queue validation.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer
from backend.app.services.printer_manager import printer_manager

router = APIRouter(prefix="/voice-commands", tags=["voice-commands"])


class VoiceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["print"]
    letters: list[str] = Field(min_length=1, max_length=50)
    bambuddy_printer: str = Field(min_length=1, max_length=100)
    slot: int | None = Field(default=None, ge=1, le=4)
    quantity: int = Field(default=1, ge=1, le=50)
    needs_clarification: bool = False
    clarification: str | None = None

    @field_validator("letters")
    @classmethod
    def letters_are_latin_capitals(cls, value: list[str]) -> list[str]:
        if any(len(letter) != 1 or not ("A" <= letter <= "Z") for letter in value):
            raise ValueError("letters must contain only Latin capital letters A-Z")
        return value


class VoiceCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[VoiceEntry] = Field(min_length=1, max_length=50)


class VoiceEntryResult(BaseModel):
    index: int
    status: Literal["accepted", "rejected"]
    reason: str | None = None
    printer_id: int | None = None
    matched_files: list[str] = []
    queue_item_id: int | None = None


class VoiceCommandResponse(BaseModel):
    dry_run: bool = True
    results: list[VoiceEntryResult]


def _printer_has_ams(printer_id: int) -> bool:
    """Use the printer's live Bambuddy state, never the voice client's claim."""
    client = printer_manager._clients.get(printer_id)  # live state owned by manager
    raw = client.state.raw_data if client and client.state else printer_manager.last_known_trays(printer_id)
    return isinstance((raw or {}).get("ams"), list)


@router.post("", response_model=VoiceCommandResponse)
async def validate_voice_command(
    command: VoiceCommandRequest,
    db: AsyncSession = Depends(get_db),
    _current_user=RequirePermissionIfAuthEnabled(Permission.QUEUE_CREATE),
):
    """Validate a structured voice command without queueing or printing anything."""
    printers = (await db.execute(select(Printer).where(Printer.is_active == True))).scalars().all()  # noqa: E712
    printers_by_name = {printer.name.casefold(): printer for printer in printers}
    files = (await db.execute(LibraryFile.active())).scalars().all()
    letters = {
        file.filename[:-4].strip().upper(): file.filename
        for file in files
        if file.filename.lower().endswith(".3mf") and not file.filename.lower().endswith(".gcode.3mf")
    }

    results: list[VoiceEntryResult] = []
    for index, entry in enumerate(command.entries):
        if entry.needs_clarification or entry.clarification:
            results.append(VoiceEntryResult(index=index, status="rejected", reason="Command needs clarification"))
            continue
        printer = printers_by_name.get(entry.bambuddy_printer.strip().casefold())
        if printer is None:
            results.append(VoiceEntryResult(index=index, status="rejected", reason="Unknown or inactive printer"))
            continue
        has_ams = _printer_has_ams(printer.id)
        if has_ams and entry.slot is None:
            results.append(VoiceEntryResult(index=index, status="rejected", reason="An AMS slot is required for this printer", printer_id=printer.id))
            continue
        if not has_ams and entry.slot is not None:
            results.append(VoiceEntryResult(index=index, status="rejected", reason="This printer has no AMS; slot must be null", printer_id=printer.id))
            continue
        missing = [letter for letter in entry.letters if letter not in letters]
        if missing:
            results.append(VoiceEntryResult(index=index, status="rejected", reason=f"Letter files not found: {', '.join(missing)}", printer_id=printer.id))
            continue
        results.append(VoiceEntryResult(
            index=index,
            status="accepted",
            printer_id=printer.id,
            matched_files=[letters[letter] for letter in entry.letters],
        ))
    return VoiceCommandResponse(results=results)
