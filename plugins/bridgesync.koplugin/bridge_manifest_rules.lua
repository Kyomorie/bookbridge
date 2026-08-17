local ManifestRules = {}

-- revisionToPersist returns the manifest revision string that should be persisted
-- after a sweep. If the sweep had any download errors OR any downloads deferred by
-- the per-sync cap, we must NOT persist the server's revision — otherwise the
-- revision-match short-circuit on the next sync would skip the manifest walk
-- entirely and those failed/deferred downloads would never be retried. In that
-- "dirty" case we return the empty string "" to force a re-walk on the next sync.
function ManifestRules.revisionToPersist(remote_revision, errors, remaining)
    local err_count = tonumber(errors) or 0
    local rem_count = tonumber(remaining) or 0
    if err_count > 0 or rem_count > 0 then
        return ""
    end
    return remote_revision or ""
end

-- downloadAllowed returns true if a download should proceed under the per-sync cap.
-- cap nil, non-numeric, 0, or negative means unlimited -> always true.
-- Otherwise returns true while attempts (nil coerced to 0) is strictly less than cap.
function ManifestRules.downloadAllowed(attempts, cap)
    local cap_num = tonumber(cap)
    if not cap_num or cap_num <= 0 then
        return true
    end
    local attempt_num = tonumber(attempts) or 0
    return attempt_num < cap_num
end

return ManifestRules