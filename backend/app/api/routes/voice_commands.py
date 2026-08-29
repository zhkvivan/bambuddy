"""Local, validated voice-command intake.

The speech recogniser is an untrusted client.  It can request a letter print,
but it can never select arbitrary paths, invent printers, or bypass Bambuddy's
normal slicer and queue checks.
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.library import save_3mf_bytes_to_library, slice_and_persist
from backend.app.api.routes.print_queue import add_to_queue
from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.config import settings as app_settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.schemas.slicer import PresetRef, SliceRequest
from backend.app.services.design_settings import extract_design_process_overrides
from backend.app.services.printer_manager import printer_manager
from backend.app.services.slicer_3mf_convert import extract_source_printer_model
from backend.app.services.three_mf_merge import merge_projects_on_plate
from backend.app.utils.printer_models import normalize_printer_model

router = APIRouter(prefix="/voice-commands", tags=["voice-commands"])


class VoiceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Voice parsers may omit the only currently supported action.  Keep the
    # contract ergonomic while still rejecting any explicit unknown action.
    action: Literal["print"] = "print"
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
    # Kept for a safe operational check and for future voice-client previews.
    # The voice listener omits both fields, so normal commands are real jobs.
    dry_run: bool = False
    manual_start: bool = False


class VoiceEntryResult(BaseModel):
    index: int
    status: Literal["accepted", "rejected"]
    reason: str | None = None
    printer_id: int | None = None
    matched_files: list[str] = []
    queue_item_id: int | None = None


class VoiceCommandResponse(BaseModel):
    dry_run: bool
    results: list[VoiceEntryResult]


def _printer_has_ams(printer_id: int) -> bool:
    """Use the printer's live Bambuddy state, never the voice client's claim."""
    client = printer_manager._clients.get(printer_id)  # live state owned by manager
    raw = client.state.raw_data if client and client.state else printer_manager.last_known_trays(printer_id)
    ams_units = (raw or {}).get("ams")
    return isinstance(ams_units, list) and len(ams_units) > 0


def _printer_raw_state(printer_id: int) -> dict:
    """Return Bambuddy's own live/remembered printer state."""
    client = printer_manager._clients.get(printer_id)
    raw = client.state.raw_data if client and client.state else printer_manager.last_known_trays(printer_id)
    return raw if isinstance(raw, dict) else {}


def _global_tray_for_voice_slot(printer_id: int, slot: int) -> int | None:
    """Resolve a human 1-based slot to Bambuddy's global tray identifier."""
    for ams in _printer_raw_state(printer_id).get("ams") or []:
        trays = ams.get("tray") or []
        if not isinstance(trays, list) or len(trays) < slot:
            continue
        tray = trays[slot - 1]
        # A selected empty slot is not a valid print target.  Letting it reach
        # the scheduler would make a voice command appear accepted then fail
        # later with a much less useful printer-side error.
        if not isinstance(tray, dict) or not tray.get("tray_type"):
            return None
        try:
            ams_id = int(ams.get("id", 0))
            tray_id = int(tray.get("id", slot - 1))
        except (TypeError, ValueError):
            return None
        return ams_id if ams_id >= 128 else ams_id * 4 + tray_id
    return None


def _printer_voice_key(name: str) -> str:
    """Match the human voice name while ignoring UI ordering prefixes.

    Operators often number their printer cards (``2 Archie``) to control
    layout.  That number is not part of the spoken identity, but the final
    resolved row is still Bambuddy's own active-printer record.
    """
    return re.sub(r"^\s*\d+\s+", "", name).strip().casefold()


def _slice_presets_for(printer: Printer) -> tuple[PresetRef, PresetRef, PresetRef]:
    """Use the same stock 0.4 mm / PLA defaults as the fast printer UI path."""
    is_p1s = printer.model.strip().upper() == "P1S"
    return (
        PresetRef(source="standard", id="Bambu Lab P1S 0.4 nozzle" if is_p1s else "Bambu Lab A1 0.4 nozzle"),
        # The bundled P1S 0.4 setup shares Bambu Studio's X1C process
        # profile; the P1P-labelled variant is rejected by this slicer's
        # 3MF compatibility gate when retargeting an A1 project.
        PresetRef(source="standard", id="0.20mm Standard @BBL X1C" if is_p1s else "0.20mm Standard @BBL A1"),
        PresetRef(source="standard", id="Bambu PLA Basic @BBL P1S 0.4 nozzle" if is_p1s else "Bambu PLA Basic @BBL A1"),
    )


def _library_path(file: LibraryFile):
    from pathlib import Path

    path = Path(file.file_path)
    return path if path.is_absolute() else Path(app_settings.base_dir) / path


def _can_slice_as_designed(model_bytes: bytes, printer: Printer) -> bool:
    """Only apply a project's full settings to the same printer family.

    Bambu Studio's “use file settings” path includes machine-specific bed,
    start G-code and motion parameters.  Those must never be copied from an
    A1 project to a P1S.  On a matching machine it is exactly the operator's
    saved recipe, including walls, infill and variable layer heights.
    """
    return normalize_printer_model(extract_source_printer_model(model_bytes)) == normalize_printer_model(printer.model)


def _portable_design_overrides(model_bytes: bytes) -> list[str]:
    """Keep every model setting that remains meaningful after a printer swap.

    This is the no-dialog equivalent of carrying the source 3MF's design
    settings in the SliceModal.  Variable/adaptive layer heights live in the
    project itself; this list additionally preserves process choices such as
    wall count, infill, supports and layer-height intent.  Kinematics, thermal
    limits and start G-code deliberately stay with the target P1S profile.
    """
    return [override.key for override in extract_design_process_overrides(model_bytes) if not override.printer_coupled]


@router.post("", response_model=VoiceCommandResponse)
async def submit_voice_command(
    command: VoiceCommandRequest,
    db: AsyncSession = Depends(get_db),  # noqa: B008 - FastAPI dependency declaration
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.QUEUE_CREATE),  # noqa: B008 - FastAPI dependency declaration
):
    """Validate all entries, then slice and queue them in spoken order.

    Validation deliberately happens as a complete first pass: one bad voice
    entry means *no* print job is created.  This is essential for commands such
    as “print A on Alpha, then B on Atom slot three”.
    """
    printers = (await db.execute(select(Printer).where(Printer.is_active == True))).scalars().all()
    printers_by_name = {_printer_voice_key(printer.name): printer for printer in printers}
    # Voice JSON intentionally has no arbitrary filesystem path.  Match the
    # normal printer-first UI's default letter set instead of letting duplicate
    # A.3mf files from Lego / large / skin folders resolve by DB row order.
    standard_folder = (await db.execute(
        select(LibraryFolder).where(LibraryFolder.name.ilike("%standard%"))
    )).scalars().first()
    files = []
    if standard_folder is not None:
        files = (await db.execute(
            LibraryFile.active().where(LibraryFile.folder_id == standard_folder.id)
        )).scalars().all()
    letters = {
        file.filename[:-4].strip().upper(): file
        for file in files
        if file.filename.lower().endswith(".3mf") and not file.filename.lower().endswith(".gcode.3mf")
    }

    results: list[VoiceEntryResult] = []
    prepared: list[tuple[int, VoiceEntry, Printer, list[LibraryFile], int | None]] = []
    for index, entry in enumerate(command.entries):
        if entry.needs_clarification or entry.clarification:
            results.append(VoiceEntryResult(index=index, status="rejected", reason="Command needs clarification"))
            continue
        printer = printers_by_name.get(_printer_voice_key(entry.bambuddy_printer))
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
        global_tray_id = None
        if entry.slot is not None:
            global_tray_id = _global_tray_for_voice_slot(printer.id, entry.slot)
            if global_tray_id is None:
                results.append(VoiceEntryResult(
                    index=index,
                    status="rejected",
                    reason=f"AMS slot {entry.slot} is empty or unavailable",
                    printer_id=printer.id,
                ))
                continue
        if standard_folder is None:
            results.append(VoiceEntryResult(index=index, status="rejected", reason="No default Standard letter folder is configured", printer_id=printer.id))
            continue
        missing = [letter for letter in entry.letters if letter not in letters]
        if missing:
            results.append(VoiceEntryResult(index=index, status="rejected", reason=f"Letter files not found: {', '.join(missing)}", printer_id=printer.id))
            continue
        results.append(VoiceEntryResult(
            index=index,
            status="accepted",
            printer_id=printer.id,
            matched_files=[letters[letter].filename for letter in entry.letters],
        ))
        prepared.append((index, entry, printer, [letters[letter] for letter in entry.letters], global_tray_id))

    # Atomic at the validation boundary: never leave an earlier spoken command
    # in the queue when a later one is malformed.
    if any(result.status == "rejected" for result in results) or command.dry_run:
        return VoiceCommandResponse(dry_run=command.dry_run, results=results)

    # Build every sliced source before creating the first queue item.  A slicer
    # failure therefore cannot create a partial sequence of real print jobs.
    sliced_sources: list[tuple[int, VoiceEntry, Printer, list[LibraryFile], int | None, int]] = []
    try:
        for index, entry, printer, source_files, global_tray_id in prepared:
            if len(source_files) == 1:
                model_file = source_files[0]
                model_path = _library_path(model_file)
                model_bytes = model_path.read_bytes()
                model_filename = model_file.filename
                folder_id = model_file.folder_id
            else:
                projects = [_library_path(source).read_bytes() for source in source_files]
                merged = merge_projects_on_plate(projects)
                merged_name = "voice-" + "-".join(entry.letters) + ".3mf"
                model_file, _ = await save_3mf_bytes_to_library(
                    db,
                    file_bytes=merged,
                    filename=merged_name,
                    folder_id=source_files[0].folder_id,
                    source_type="voice-merged",
                    owner_id=current_user.id if current_user else None,
                )
                model_bytes = merged
                model_filename = model_file.filename
                folder_id = model_file.folder_id

            printer_preset, process_preset, filament_preset = _slice_presets_for(printer)
            use_embedded_settings = _can_slice_as_designed(model_bytes, printer)
            design_overrides = [] if use_embedded_settings else _portable_design_overrides(model_bytes)
            slice_result = await slice_and_persist(
                db,
                model_bytes=model_bytes,
                model_filename=model_filename,
                folder_id=folder_id,
                extra_metadata={"voice_command": True},
                request=SliceRequest(
                    printer_preset=printer_preset,
                    process_preset=process_preset,
                    filament_preset=filament_preset,
                    filament_presets=[filament_preset],
                    # A single saved letter stays entirely in the author's
                    # layout.  Multi-letter words and duplicate runs need the
                    # existing automatic placement step to prevent overlap.
                    auto_arrange=len(source_files) > 1 or entry.quantity > 1,
                    copies_on_plate=entry.quantity,
                    use_embedded_settings=use_embedded_settings,
                    design_overrides=design_overrides,
                ),
                current_user_id=current_user.id if current_user else None,
            )
            sliced_sources.append((index, entry, printer, source_files, global_tray_id, slice_result.library_file_id))
    except Exception as exc:  # noqa: BLE001 - return one useful voice-command result for any slicer failure
        reason = getattr(exc, "detail", None) or str(exc) or "Failed to prepare the print"
        for result in results:
            if result.status == "accepted":
                result.status = "rejected"
                result.reason = f"Could not prepare command: {reason}"
        return VoiceCommandResponse(dry_run=False, results=results)

    # Reuse the ordinary queue route for its access, printer, filename, budget
    # and scheduling checks.  Sequential awaits preserve the GPT array order.
    for index, _entry, printer, _source_files, global_tray_id, sliced_file_id in sliced_sources:
        try:
            queued = await add_to_queue(
                PrintQueueItemCreate(
                    printer_id=printer.id,
                    library_file_id=sliced_file_id,
                    ams_mapping=[global_tray_id] if global_tray_id is not None else None,
                    manual_start=command.manual_start,
                ),
                db=db,
                current_user=current_user,
            )
            results[index].queue_item_id = queued.id
        except HTTPException as exc:
            # This should be unreachable after the preflight checks, but return
            # a useful per-entry result rather than turning a speech command into
            # an opaque 500.  Earlier rows are already real, normal queue items.
            results[index].status = "rejected"
            results[index].reason = str(exc.detail)

    return VoiceCommandResponse(dry_run=False, results=results)
