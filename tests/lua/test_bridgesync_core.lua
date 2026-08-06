local plugin_dir = assert(arg[1], "plugin directory argument required")
package.path = plugin_dir .. "/?.lua;" .. package.path

package.preload["docsettings"] = function() return {} end
package.preload["libs/libkoreader-lfs"] = function() return { attributes = function() return nil end } end
package.preload["logger"] = function()
    return { info = function() end, warn = function() end, err = function() end }
end
package.preload["ffi/sha2"] = function()
    return { md5 = function(value) return tostring(value):sub(1, 32) end }
end
package.preload["datastorage"] = function()
    return { getSettingsDir = function() return "/tmp/bridgesync-test" end }
end
-- bridge_sqlite_state only touches json for table-typed values; the contract
-- tests below use scalars only, so hitting json here means a test regressed.
package.preload["json"] = function()
    return {
        encode = function() error("json.encode must not be reached by scalar tests") end,
        decode = function() error("json.decode must not be reached by scalar tests") end,
    }
end

-- Faithful fake of KOReader's lua-ljsqlite3 statement/connection API:
--   * bind(...) binds all varargs positionally; bind1(i, v) binds one index
--   * step() pops scripted rows and returns nil when drained (SQLITE_DONE)
--   * statements are released with close(); there is no finalize()
local fake_sq3
do
    local Stmt = {}
    Stmt.__index = Stmt
    function Stmt:bind1(i, v)
        self.params[i] = v
        return self
    end
    function Stmt:bind(...)
        for i = 1, select("#", ...) do
            self:bind1(i, (select(i, ...)))
        end
        return self
    end
    function Stmt:step()
        return table.remove(self.rows, 1)
    end
    function Stmt:reset()
        return self
    end
    function Stmt:close()
        self.closed = true
    end

    local Conn = {}
    Conn.__index = Conn
    function Conn:prepare(sql)
        local stmt = setmetatable({
            sql = sql,
            params = {},
            rows = table.remove(self.scripted_rows, 1) or {},
            closed = false,
        }, Stmt)
        table.insert(self.prepared, stmt)
        return stmt
    end
    function Conn:exec(sql)
        table.insert(self.executed, sql)
    end
    function Conn:close()
        self.conn_closed = true
    end
    function Conn:script_rows(rows)
        table.insert(self.scripted_rows, rows)
    end

    fake_sq3 = {
        new_conn = function()
            return setmetatable({
                prepared = {},
                executed = {},
                scripted_rows = {},
            }, Conn)
        end,
    }
    function fake_sq3.open()
        fake_sq3.last_conn = fake_sq3.new_conn()
        return fake_sq3.last_conn
    end
end
package.preload["lua-ljsqlite3/init"] = function() return fake_sq3 end

local annotations = require("bridge_annotations")
local coordinator_module = require("bridge_sync_coordinator")
local stats_batches = require("bridge_stats_batches")
local version = require("bridge_version")
local sessions = require("bridge_sessions")
local BridgeSqliteState = require("bridge_sqlite_state")

local entries = {}
for index = 1, 55 do
    entries[index] = {
        datetime = "2020-01-01 00:00:00",
        datetime_updated = "2020-01-01 00:00:00",
        drawer = "lighten",
        text = "highlight " .. index,
        pos0 = "/body/p[" .. index .. "]/text().0",
        pos1 = "/body/p[" .. index .. "]/text().5",
    }
end

local saved_watermarks = {}
local exchange_calls = 0
local bridge = {
    state = {
        readSetting = function(_, key)
            if key == "annotation_watermarks" then return saved_watermarks end
            return nil
        end,
        saveSetting = function(_, key, value)
            if key == "annotation_watermarks" then saved_watermarks = value end
        end,
        flush = function() end,
    },
    api = {
        exchangeAnnotations = function(_, payload)
            exchange_calls = exchange_calls + 1
            local response_books = {}
            for _, book in ipairs(payload.books) do
                table.insert(response_books, {
                    hash = book.hash,
                    toApply = { add = {}, edit = {}, delete = {} },
                    more = false,
                })
            end
            return true, { enabled = true, books = response_books }
        end,
        ackAnnotations = function() return true end,
    },
    _currentDeviceIdentity = function() return "Test", "device-1" end,
    logInfo = function() end,
    logWarn = function() end,
}

local result, exchange_err = annotations.exchangeBooks(bridge, {
    { hash = string.rep("a", 32), annotations = entries, live = false },
})
assert(result, exchange_err)
assert(result.uploaded == 55, "all annotation chunks must be uploaded")
assert(exchange_calls == 2, "55 annotations must be split across two exchanges")
assert(saved_watermarks[string.rep("a", 32)] == "2020-01-01 00:00:00",
    "watermark advances only after all same-timestamp chunks succeed")

local pages, books = {}, {}
for index = 1, 10001 do
    local hash = string.format("%032d", (index % 3) + 1)
    pages[index] = { md5 = hash, page = index }
end
for index = 1, 3 do
    books[index] = { md5 = string.format("%032d", index), title = "Book " .. index }
end
local batches = stats_batches.build(pages, books, 3000)
assert(#batches == 4, "10001 rows must produce four bounded batches")
local page_count = 0
for _, batch in ipairs(batches) do
    assert(#batch.page_stats <= 3000, "statistics batch exceeded its limit")
    page_count = page_count + #batch.page_stats
end
assert(page_count == 10001, "statistics batching lost rows")

local now = 100
local coordinator = coordinator_module:new(function() return now end)
local order = {}
local finish_first
coordinator:submit({
    family = "first", priority = 100,
    run = function(done)
        table.insert(order, "first")
        finish_first = done
    end,
})
coordinator:submit({
    family = "annotations", priority = 100,
    run = function(done) table.insert(order, "old-annotations"); done() end,
})
coordinator:submit({
    family = "annotations", priority = 200,
    run = function(done) table.insert(order, "new-annotations"); done() end,
})
coordinator:submit({
    family = "close", priority = 300,
    run = function(done) table.insert(order, "close"); done() end,
})
assert(coordinator:status().pending_count == 2, "duplicate family was not coalesced")
finish_first()
assert(table.concat(order, ",") == "first,close,new-annotations",
    "coordinator did not honor priority and replacement")
assert(not coordinator:isBusy(), "coordinator remained busy after all jobs completed")

assert(version.isNewer("0.4.0", "0.3.6"), "newer semantic version was not detected")
assert(not version.isNewer("0.3.5", "0.3.6"), "older server version would trigger a downgrade")
assert(not version.isNewer("0.3.6", "0.3.6"), "equal version was treated as newer")

-- ── ManifestRules (bridge_manifest_rules.lua) ──
local manifest_rules = require("bridge_manifest_rules")

assert(manifest_rules.revisionToPersist("abc123", 0, 0) == "abc123",
    "revisionToPersist clean sweep persists revision")
assert(manifest_rules.revisionToPersist("abc123", 1, 0) == "",
    "revisionToPersist with errors returns empty string")
assert(manifest_rules.revisionToPersist("abc123", 0, 2) == "",
    "revisionToPersist with remaining downloads returns empty string")
assert(manifest_rules.revisionToPersist("abc123", "3", nil) == "",
    "revisionToPersist coerces string errors to number")
assert(manifest_rules.revisionToPersist(nil, 0, 0) == "",
    "revisionToPersist with nil revision returns empty string")
assert(manifest_rules.revisionToPersist("abc123", nil, nil) == "abc123",
    "revisionToPersist treats nil errors/remaining as zero")

assert(manifest_rules.downloadAllowed(0, 0) == true, "downloadAllowed 0 cap is unlimited")
assert(manifest_rules.downloadAllowed(999, 0) == true, "downloadAllowed 0 cap unlimited even at high attempts")
assert(manifest_rules.downloadAllowed(0, nil) == true, "downloadAllowed nil cap is unlimited")
assert(manifest_rules.downloadAllowed(4, 5) == true, "downloadAllowed attempts under cap allowed")
assert(manifest_rules.downloadAllowed(5, 5) == false, "downloadAllowed attempts at cap denied")
assert(manifest_rules.downloadAllowed(nil, 5) == true, "downloadAllowed nil attempts treated as zero")
assert(manifest_rules.downloadAllowed(3, -1) == true, "downloadAllowed negative cap is unlimited")

-- ── Session collapsing (bridge_sessions.lua, the real module) ──

-- Adjacent sessions for the same book merge, and reading duration
-- accumulates instead of absorbing the idle gap between them.
do
    local pending = {}
    local first = {
        abs_id = "book-1", start_time = 1000, end_time = 1100,
        duration_seconds = 100, start_progress = 0, end_progress = 10, end_page = 5,
    }
    assert(not sessions.mergeIntoPending(pending, first, 300),
        "first session has nothing to merge into")
    table.insert(pending, first)

    local merged = sessions.mergeIntoPending(pending, {
        abs_id = "book-1", start_time = 1300, end_time = 1400,
        duration_seconds = 100, start_progress = 10, end_progress = 20, end_page = 10,
    }, 300)
    assert(merged, "session 200s after previous end must merge with 300s threshold")
    assert(#pending == 1, "merged sessions must collapse into one entry")
    assert(pending[1].end_time == 1400, "merge must extend end_time")
    assert(pending[1].end_progress == 20, "merge must extend end_progress")
    assert(pending[1].end_page == 10, "merge must extend end_page")
    assert(pending[1].duration_seconds == 200,
        "merged duration must be the sum of reading time, not the 1000-1400 span")
end

-- Different books never merge.
do
    local pending = {
        { abs_id = "book-1", start_time = 1000, end_time = 1100, duration_seconds = 100 },
    }
    local merged = sessions.mergeIntoPending(pending, {
        abs_id = "book-2", start_time = 1150, end_time = 1250, duration_seconds = 100,
    }, 300)
    assert(not merged, "different books must not merge")
end

-- Sessions past the threshold never merge.
do
    local pending = {
        { abs_id = "book-1", start_time = 1000, end_time = 1100, duration_seconds = 100 },
    }
    local merged = sessions.mergeIntoPending(pending, {
        abs_id = "book-1", start_time = 1500, end_time = 1600, duration_seconds = 100,
    }, 300)
    assert(not merged, "session 400s later must not merge with 300s threshold")
end

-- Hash-only sessions (no abs_id) never merge.
do
    local pending = {
        { abs_id = nil, document_hash = "h1", start_time = 1000, end_time = 1100, duration_seconds = 100 },
    }
    local merged = sessions.mergeIntoPending(pending, {
        abs_id = nil, document_hash = "h1", start_time = 1150, end_time = 1250, duration_seconds = 100,
    }, 300)
    assert(not merged, "nil abs_id sessions must always append")
end

-- Already-uploaded sessions are never merge targets.
do
    local pending = {
        { abs_id = "book-1", start_time = 1000, end_time = 1100, duration_seconds = 100, uploaded = true },
    }
    local merged = sessions.mergeIntoPending(pending, {
        abs_id = "book-1", start_time = 1150, end_time = 1250, duration_seconds = 100,
    }, 300)
    assert(not merged, "uploaded sessions must not absorb new reading time")
end

-- ── bridge_sqlite_state.lua contract tests (fake lua-ljsqlite3) ──
-- These pin the module to the real library semantics: positional bind1
-- parameters, step() returning nil on successful writes, close() cleanup.

local function newState()
    local state = BridgeSqliteState:new()
    assert(state:is_available(), "fake SQ3 must make sqlite available")
    assert(state:init(), "init must succeed against the fake connection")
    return state, fake_sq3.last_conn
end

local function lastStmt(conn)
    local stmt = conn.prepared[#conn.prepared]
    assert(stmt, "expected a prepared statement")
    assert(stmt.closed, "statements must be close()d after use")
    return stmt
end

-- Writes succeed: step() returning nil (SQLITE_DONE) is success, not failure.
do
    local state, conn = newState()
    assert(state:set_setting("server_url", "http://bridge:5758") == true,
        "set_setting must treat step()'s nil return (SQLITE_DONE) as success")
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("INSERT OR REPLACE INTO plugin_settings"),
        "set_setting must upsert plugin_settings")
    assert(stmt.params[1] == "bridgesync", "param 1 must be the plugin name")
    assert(stmt.params[2] == "server_url", "param 2 must be the key")
    assert(stmt.params[3] == "http://bridge:5758", "param 3 must be the value")
    assert(stmt.params[4] == "string", "param 4 must be the type tag")
end

-- Deleting via nil value.
do
    local state, conn = newState()
    assert(state:set_setting("stale_key", nil) == true)
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("DELETE FROM plugin_settings"), "nil value must delete the row")
    assert(stmt.params[2] == "stale_key")
end

-- Reads decode type tags; a stored boolean false survives as false.
do
    local state, conn = newState()
    conn:script_rows({ { "false", "boolean" } })
    local value = state:get_setting("auto_sync_on_close", true)
    assert(value == false,
        "stored boolean false must be returned as false, never the default")

    conn:script_rows({ { "42", "number" } })
    assert(state:get_setting("wake_sync_delay_seconds") == 42, "number decode failed")

    assert(state:get_setting("missing_key", "fallback") == "fallback",
        "missing key must return the default")
end

-- Pending sessions persist every field the bridge upload endpoint consumes.
do
    local state, conn = newState()
    local sid = state:add_pending_session({
        abs_id = "abs-1",
        document_hash = "deadbeef",
        session_type = "EPUB",
        start_time = 1000,
        end_time = 1600,
        duration_seconds = 600,
        start_page = 10,
        end_page = 25,
        start_progress = 12.5,
        end_progress = 20.0,
    })
    assert(type(sid) == "string" and sid ~= "", "add_pending_session must return an id")
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("INSERT INTO plugin_pending_sessions"))
    assert(stmt.params[1] == "bridgesync")
    assert(stmt.params[2] == sid)
    assert(stmt.params[3] == "abs-1")
    assert(stmt.params[4] == "deadbeef", "document_hash must be persisted")
    assert(stmt.params[5] == "EPUB", "session_type must be persisted")
    assert(stmt.params[6] == 1000 and stmt.params[7] == 1600)
    assert(stmt.params[8] == 600, "duration_seconds must be persisted")
    assert(stmt.params[9] == 10 and stmt.params[10] == 25)
    assert(stmt.params[11] == 12.5 and stmt.params[12] == 20.0)
end

-- Session rows come back in upload-payload shape.
do
    local state, conn = newState()
    conn:script_rows({
        { "sid-1", "abs-1", "deadbeef", "EPUB", 1000, 1600, 600, 10, 25, 12.5, 20.0, 0 },
    })
    local pending = state:get_pending_sessions(nil, false)
    assert(#pending == 1)
    local s = pending[1]
    assert(s.session_id == "sid-1" and s.abs_id == "abs-1")
    assert(s.document_hash == "deadbeef" and s.session_type == "EPUB")
    assert(s.start_time == 1000 and s.end_time == 1600 and s.duration_seconds == 600)
    assert(s.uploaded == false, "uploaded flag must decode to boolean")
    local stmt = lastStmt(conn)
    assert(stmt.params[2] == 0, "uploaded=false filter must bind 0")
end

-- Merging extends the end state and ADDS reading duration.
do
    local state, conn = newState()
    assert(state:merge_pending_session_end("sid-1", 1600, 25, 20.0, 300) == true)
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("duration_seconds = duration_seconds %+ %?"),
        "merge must accumulate duration, not overwrite it")
    assert(stmt.params[1] == 1600 and stmt.params[2] == 25 and stmt.params[3] == 20.0)
    assert(stmt.params[4] == 300, "param 4 must be the added duration")
    assert(stmt.params[6] == "sid-1")
end

-- find_mergeable guards and parameter order.
do
    local state, conn = newState()
    assert(state:find_mergeable_pending_session(nil, 1000, 300) == nil,
        "nil abs_id must never find a merge target")
    conn:script_rows({
        { "sid-2", "abs-1", nil, "EPUB", 500, 900, 400, 1, 9, 0.0, 10.0, 0 },
    })
    local found = state:find_mergeable_pending_session("abs-1", 1000, 300)
    assert(found and found.session_id == "sid-2")
    local stmt = lastStmt(conn)
    assert(stmt.params[2] == "abs-1" and stmt.params[3] == 1000 and stmt.params[4] == 300)
end

-- Batch upload marking binds ids after the plugin name.
do
    local state, conn = newState()
    assert(state:mark_sessions_uploaded({ "a", "b", "c" }) == true)
    local stmt = lastStmt(conn)
    assert(stmt.sql:find("SET uploaded = 1"))
    assert(stmt.params[1] == "bridgesync")
    assert(stmt.params[2] == "a" and stmt.params[3] == "b" and stmt.params[4] == "c")
    assert(state:mark_sessions_uploaded({}) == false, "empty batch must be a no-op failure")
end

print("BridgeSync Lua core tests passed")
