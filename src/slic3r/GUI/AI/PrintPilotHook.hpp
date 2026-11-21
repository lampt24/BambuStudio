#pragma once
// PrintPilotHook.hpp
// Pre-send hook: exports a temporary 3MF, runs the PrintPilot analysis flow,
// and returns whether the send should proceed.
//
// Owned exclusively by src/slic3r/GUI/AI/.
// Called from Plater.cpp send handlers (minimal hooks only).

namespace Slic3r { namespace GUI {
class Plater;
namespace AI {
extern thread_local bool redacting_export;

// trigger: "send" (called before PrintAll/PrintPlate/Send/MultiSend actions).
// Returns true  → continue with the send as normal.
// Returns false → abort the send (user applied AI changes and must reslice,
//                 or user cancelled).
//
// This function:
//   - Exports a silent temp 3MF to a process-unique path in the system temp dir.
//   - Calls run_printpilot_flow().
//   - Cleans up the temp file.
//   - Shows a "reslice required" notification if changes were applied.
//   - Never starts, pauses, or stops a real printer.
//
// Always called on the wx main thread.
bool run_before_send(Plater* plater, bool isall);
bool run_before_queue_send();

// Manual trigger from the AI button (not before send).
void run_manual(Plater* plater);

}}} // namespace Slic3r::GUI::AI
