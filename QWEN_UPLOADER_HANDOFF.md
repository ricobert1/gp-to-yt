photos2yt — Uploader Rewrite Technical Handoff
Objective

Produce a production-quality replacement for the existing YouTube uploader.

The existing project files are the source of truth. Inspect them before modifying anything.

The implementation must preserve compatibility with:

db.py — schema version 3 and existing state machine
config.py — existing configuration dataclasses
quota.py — existing QuotaTracker
auth.py — existing OAuth credentials
Existing CLI commands and tests

Do not rewrite unrelated modules unnecessarily.

1. Required architecture

Separate the implementation into three layers.

Database layer

Responsible for:

Video records
State transitions
Session URI persistence
Verification results
Quota tracking
Resumable transport layer

Responsible for:

Creating YouTube upload sessions
Sending chunks
Querying acknowledged byte ranges
Resuming interrupted sessions
Handling network failures

Suggested module:

photos2yt/resumable.py

Uploader orchestration layer

Responsible for:

Queue processing
Quota checks
Metadata construction
Crash recovery
Duplicate detection
Error classification
Post-upload verification

Suggested module:

photos2yt/uploader.py

2. Database additions

Add:

update_upload_session(conn, video_id, session_uri)

Requirements:

Only update records in UPLOADING status.
Persist the session URI immediately after session creation.
Support clearing an expired or completed session.
Never log the session URI.

Add:

update_video_verification(conn, video_id, actual_privacy, verification_error=None)

Requirements:

Update verification metadata without changing lifecycle status.
Do not mark an uploaded video FAILED merely because verification is temporarily unavailable.
Preserve the existing schema unless a genuinely necessary migration is justified.

Keep the six existing statuses:

PENDING, UPLOADING, UPLOADED, PLAYLISTED, FAILED, SKIPPED.

3. Resumable upload protocol

Use Google's documented YouTube resumable upload protocol.

Official documentation:

https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol

Requirements:

Initiate a session using POST.
Save the returned Location header immediately.
Upload the file using PUT requests.
Use Content-Range correctly.
Respect the 256 KiB chunk alignment requirement.
Handle HTTP 308 Resume Incomplete.
Read the Range header to determine acknowledged bytes.
Query session status after uncertain network failures.
Respect Retry-After when supplied.
Handle 404 expired sessions.
Handle 500, 502, 503 and 504 with exponential backoff.
Recognize successful completion responses (normally HTTP 201).

Never assume the server received the entire previous chunk.

Never resend a chunk without first determining the server's acknowledged byte range after an uncertain response.

Check for a replacement Location header and use it if supplied.

Do not automatically retry permanent API errors.

Do not expose session URIs in logs or exceptions.

The previously supplied resumable.py reference code is a starting point, not an approved production implementation. Review and correct its exception handling, resource management and edge cases.

4. Source file integrity

Before creating or resuming an upload:

Verify the source file exists.
Verify its size matches VideoRecord.size_bytes.
Verify its SHA-256 matches VideoRecord.sha256.
Reject a changed source file.
Never resume a session using a different file.

Consider verifying the file again after upload to detect modifications during transfer.

The source file should remain immutable during an upload.

5. Crash recovery

Implement:

_recover_interrupted_uploads()

Recovery should:

Find videos in UPLOADING status.
Check whether a session URI exists.
Verify source file integrity.
Query the saved session.
Resume if the session remains valid.
Recover a completed video resource if the session already finished.
Handle expired sessions separately.
Check the uploads playlist for possible completed uploads if the session is unavailable.
Never adopt a video solely because its title matches.

Playlist discovery:

Obtain the channel's uploads playlist through channels.list(mine=True, part="contentDetails").
Enumerate playlistItems.list pages.
Do not use a nonexistent q parameter on playlistItems.list.
Retrieve candidate video details where necessary.
Compare multiple attributes such as title, description, duration and upload timing.
Adopt only a uniquely convincing candidate.
Leave ambiguous candidates for manual reconciliation.

Use the saved resumable session as the primary recovery mechanism. Playlist discovery is a fallback.

6. Upload metadata

Implement _build_upload_body().

Requirements:

Use the existing VideoRecord fields.
Use YouTubeConfig privacy, category_id and made_for_kids.
Parse tags_json into a real list of strings.
Do not send an empty string as tags.
Include recordingDetails when valid.
Permit zero latitude and longitude.
Handle invalid dates gracefully.
Respect title_template and default_title according to existing project behavior.

Inspect the ingestion code before changing title generation.

Also: photos2yt/resumable.py and photos2yt/tests/test_uploader.py are leftovers from an earlier attempt. Delete both (and any imports of them) unless the grep shows something legitimately depends on them; if you keep any of resumable.py's ideas, fold them into uploader.py. Also create the branch first: `git switch -c task6-rewrite`.

7. Quota management

Preserve the existing QuotaTracker API.

Check quota before every new upload session.

Distinguish:

API quota exhaustion
Channel upload limit exceeded
Other permission errors
Transient network failures

Important current YouTube quota distinction:

videos.insert has a separate quota bucket, with a default 100 calls per day and a cost of 1 unit per call under the current documentation.

Other API methods use a separate quota allocation.

Documentation:

https://developers.google.com/youtube/v3/determine_quota_cost

Inspect the actual config.yaml values and design quota accounting accordingly.

Do not silently change upload_cost_units or daily_upload_limit.

Account for API operations consistently, including verification and playlist operations where appropriate.

Record upload attempts at the correct point in the lifecycle. Do not wait until final verification to account for a successfully created upload session.

Avoid double-counting when resuming an existing session.

On quotaExceeded:

Call quota_tracker.mark_exhausted().
Return the video to PENDING when appropriate.
Preserve the distinction between API quota and channel limits.
Stop the upload run.
8. Post-upload verification

Implement:

_verify_uploaded_video()

Use:

videos.list( part="status,processingDetails", id=video_id )

Verify:

Video exists.
Privacy status matches configuration.
Processing status is available.
Processing errors are identified.
Temporary processing states are handled separately.

Expected processing states include:

processing
succeeded
failed
terminated

Do not treat processing as upload failure.

Do not automatically change privacy settings without explicit application policy.

Store actual privacy in the existing actual_privacy field.

A video successfully uploaded but still processing remains UPLOADED.

Verification should be repeatable without creating another upload.

9. Error handling

Use specific exceptions for:

QuotaExhaustedError
UploadLimitExceededError
UploadSessionExpired
UploadApiError
ResumableUploadError

Retry only transient failures.

Permanent API errors should not be retried blindly.

Preserve session URIs when the session may still be valid.

Never swallow UploadApiError or UploadSessionExpired inside a generic retry handler.

Use bounded exponential backoff.

Handle HTTP 403 errors according to their actual API reason, not merely the status code.

10. Queue processing

Implement:

upload_video(conn, video_record, credentials, config, quota_tracker)

The VideoRecord must be passed directly into the function.

Never call:

get_video_by_source_path(conn, None)

Implement:

upload_videos(...)

Requirements:

Fetch pending videos using get_videos_by_status().
Process in database order.
Respect limit.
Support dry_run.
Support wait for quota reset.
Stop on quota exhaustion.
Stop on channel upload limit.
Continue processing after recoverable individual-video failures.
Return the number of successful uploads.

Preserve the existing CLI interface.

11. Testing requirements

Use mocked HTTP responses and mocked YouTube API calls.

Create or extend:

test_uploader.py
test_resumable.py
test_upload_recovery.py
test_upload_verification.py
test_upload_quota.py

Mandatory test cases:

Resumable protocol
Session creation.
Session URI persistence.
Valid chunk upload.
HTTP 308 with Range header.
Missing Range header.
Partial acknowledgment.
Lost response after server receives data.
Replacement Location header.
Retry-After.
Session query after network failure.
Expired session.
Successful completion.
Malformed completion response.
Invalid chunk size.
Unexpected session URI host.
Recovery
Resume a valid existing session.
Recover a session that already completed.
Expired session.
Missing session URI.
Source file missing.
Source size changed.
Source SHA-256 changed.
Duplicate candidate found.
Ambiguous duplicate candidates.
No duplicate found.
Quota
Sufficient quota.
Insufficient quota.
Exhausted quota.
QuotaExceeded response.
UploadLimitExceeded response.
No double-counting on resumed uploads.
Verification quota accounting.
Verification
Video exists and privacy matches.
Video is private unexpectedly.
Video is processing.
Processing succeeds.
Processing fails.
Video temporarily unavailable.
Verification request fails.
Verification does not create a duplicate upload.
12. Security and reliability
Never log OAuth access tokens.
Never log resumable session URIs.
Never expose credential contents in exceptions.
Validate session URI hosts before sending authenticated requests.
Avoid leaking sensitive upload metadata.
Ensure database updates are transactional.
Prevent concurrent upload workers from processing the same record.
Avoid duplicate upload sessions.
Preserve useful diagnostic errors without exposing secrets.
13. Completion criteria

Do not declare the rewrite finished until:

All existing tests pass.
All new tests pass.
No placeholder functions remain.
No get_video_by_source_path(conn, None) calls remain.
No invalid YouTube API parameters remain.
Resumable uploads can survive process termination.
Completed uploads are not uploaded again.
Verification is independent of upload success.
Quota exhaustion stops the queue correctly.
CLI compatibility is preserved.

Run the complete test suite and report:

Files changed.
New functions.
Tests added.
Tests passing/failing.
Dependencies added.
Remaining limitations.
Any manual configuration changes required.

Do not merely provide a proposed implementation. Make the changes in the project.

Final instruction

Inspect the repository first.

Use the supplied reference implementations as guidance, not unquestionable source code.

Prioritize correctness, recoverability and data integrity over clever abstractions.

Do not make destructive database changes or perform real YouTube uploads during testing.