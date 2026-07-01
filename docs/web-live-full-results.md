# Web Live Full Results

The web UI receives live session events over `/api/v1/ws`. To keep large command
results from blocking beacon handlers or slow browser sockets, the server still
truncates oversized WebSocket event bodies before broadcasting them.

When the browser receives a live event with `body_truncated: true`, it now fetches
the same event by id from `GET /api/v1/sessions/{id}/events`. The REST transcript
stores the full event body, so the console renders the complete output while the
live WebSocket path keeps its bounded payload size.

This behavior applies only to live web console rendering. Archived transcripts
and REST event polling already read from the stored event transcript.
