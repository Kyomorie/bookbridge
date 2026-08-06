local plugin_dir = assert(arg[1], "plugin directory argument required")
local settings_dir = assert(arg[2], "settings directory argument required")
package.path = plugin_dir .. "/?.lua;" .. package.path

local function preload(name, factory)
    package.preload[name] = factory
end

local function empty_module()
    return {}
end

preload("ui/widget/confirmbox", empty_module)
preload("ui/widget/infomessage", empty_module)
preload("ui/widget/inputdialog", empty_module)
preload("ui/network/manager", empty_module)
preload("ui/trapper", empty_module)
preload("bit", empty_module)
preload("socket", empty_module)

preload("gettext", function()
    return function(value) return value end
end)

preload("datastorage", function()
    return {
        getSettingsDir = function() return settings_dir end,
    }
end)

preload("dispatcher", function()
    return {
        registerAction = function() end,
    }
end)

preload("ui/uimanager", function()
    return {
        scheduleIn = function() end,
    }
end)

preload("ui/widget/container/widgetcontainer", function()
    local WidgetContainer = {}
    function WidgetContainer:extend(definition)
        definition.__index = definition
        function definition:new(instance)
            return setmetatable(instance or {}, self)
        end
        return definition
    end
    return WidgetContainer
end)

preload("logger", function()
    return {
        info = function() end,
        warn = function() end,
        err = function() end,
    }
end)

preload("libs/libkoreader-lfs", function()
    return {
        attributes = function(path, attribute)
            if path == "/mnt/onboard" and attribute == "mode" then
                return "directory"
            end
            return nil
        end,
    }
end)

preload("ffi/sha2", function()
    return { md5 = function(value) return value end }
end)

preload("ffi/util", function()
    return {
        template = function(value) return value end,
    }
end)

preload("string.buffer", function()
    return {
        encode = function(value) return value end,
        decode = function(value) return value end,
    }
end)

preload("json", function()
    return {
        encode = function() return "{}" end,
        decode = function(value)
            if value == "__partial_session_response__" then
                return {
                    accepted = 1,
                    rejected = 1,
                    results = {
                        { index = 1, session_id = "session-accepted", accepted = true },
                        { index = 2, session_id = "session-rejected", accepted = false,
                            reason = "book_not_found" },
                    },
                }
            end
            return {}
        end,
    }
end)

preload("luasettings", function()
    local Settings = {}
    Settings.__index = Settings
    function Settings:readSetting()
        return nil
    end
    function Settings:saveSetting(key, value)
        self.data[key] = value
    end
    function Settings:flush() end

    return {
        open = function()
            return setmetatable({ data = {} }, Settings)
        end,
    }
end)

local uploaded_log_payloads = {}

preload("bridge_api_client", function()
    local APIClient = {}
    function APIClient:new()
        return setmetatable({}, { __index = self })
    end
    function APIClient:init(server_url, username, key, log_callback)
        self.server_url = server_url
        self.username = username
        self.key = key
        self.log_callback = log_callback
    end
    function APIClient:uploadSessions()
        return true, 200, ""
    end
    function APIClient:uploadClientLogs(payload)
        uploaded_log_payloads[#uploaded_log_payloads + 1] = payload
        return true, 200, ""
    end
    return APIClient
end)

local sqlite_values = {
    server_url = "http://bridge:5758",
    username = "reader",
    key = "secret",
}

preload("bridge_sqlite_state", function()
    local BridgeSqliteState = {}
    function BridgeSqliteState:new()
        return setmetatable({}, { __index = self })
    end
    function BridgeSqliteState:is_available()
        return true
    end
    function BridgeSqliteState:init()
        return true
    end
    function BridgeSqliteState:get_setting(key, default)
        if key == "migration_done" then
            return true
        end
        if sqlite_values[key] ~= nil then
            return sqlite_values[key]
        end
        return default
    end
    function BridgeSqliteState:set_setting(key, value)
        sqlite_values[key] = value
        return true
    end
    function BridgeSqliteState:prune_uploaded_sessions()
        return true
    end
    function BridgeSqliteState:get_pending_sessions()
        return {}
    end
    return BridgeSqliteState
end)

preload("bridge_sync_coordinator", function()
    local Coordinator = {}
    function Coordinator:new()
        return setmetatable({}, { __index = self })
    end
    return Coordinator
end)

preload("bridge_annotations", empty_module)
preload("bridge_sweep", empty_module)
preload("bridge_stats_batches", empty_module)
preload("bridge_version", empty_module)
preload("bridge_sessions", empty_module)

local BridgeSync = require("main")
local bridge = BridgeSync:new({
    path = plugin_dir,
    ui = {
        menu = {
            registerToMainMenu = function() end,
        },
    },
})

local ok, init_error = pcall(bridge.init, bridge)
assert(ok, "BridgeSync init failed: " .. tostring(init_error))
assert(bridge.log_path == settings_dir .. "/bridge_sync.log",
    "BridgeSync must initialize log_path before startup logging")

local handle = assert(io.open(bridge.log_path, "r"),
    "BridgeSync startup did not create bridge_sync.log")
local log_contents = handle:read("*a")
handle:close()
assert(log_contents:find("SQLite state manager initialized", 1, true),
    "BridgeSync startup did not persist its first SQLite log message")

bridge:logWarn("Book sync completed with one deferred download")
assert(bridge:_uploadDeviceLogTail("book_sync", "partial") == true)
assert(#uploaded_log_payloads == 1)
assert(uploaded_log_payloads[1].operation == "book_sync")
assert(uploaded_log_payloads[1].status == "partial")

bridge.pending_sessions = {
    { session_id = "session-1", abs_id = "book-1" },
}
bridge.sqlite_state.mark_sessions_uploaded = function()
    return false
end
local upload_ok = bridge:_uploadSessions()
assert(upload_ok == false,
    "session upload must fail locally when SQLite acknowledgement cannot be persisted")
assert(#bridge.pending_sessions == 1,
    "unacknowledged sessions must remain queued for retry")

bridge.sqlite_state.mark_sessions_uploaded = function()
    return true
end
upload_ok = bridge:_uploadSessions()
assert(upload_ok == true,
    "session upload must complete once SQLite acknowledgement succeeds")
assert(#bridge.pending_sessions == 0,
    "acknowledged sessions must be removed from the in-memory queue")
assert(#uploaded_log_payloads == 3,
    "each attempted session upload must report its device log tail")
assert(uploaded_log_payloads[2].operation == "session_upload")
assert(uploaded_log_payloads[2].status == "failure")
assert(uploaded_log_payloads[3].status == "success")
assert(uploaded_log_payloads[3].plugin_version == "0.5.5")
assert(type(sqlite_values.device_log_upload_offset) == "number",
    "successful telemetry must persist the acknowledged log byte offset")

local saw_ack_failure = false
for _, line in ipairs(uploaded_log_payloads[2].lines or {}) do
    if line:find("local SQLite acknowledgement failed", 1, true) then
        saw_ack_failure = true
        break
    end
end
assert(saw_ack_failure,
    "failure telemetry must include the local SQLite acknowledgement diagnostic")

bridge._loadStateItems = function()
    return {
        ["kobo-book"] = {
            local_path = "/mnt/onboard/Koreaderbooks/Title.epub",
            filename = "Title.epub",
            content_hash = "hash-1",
        },
    }
end
assert(bridge:_resolveAbsId("/mnt/onboard/KoreaderBooks/Title.epub") == "kobo-book",
    "managed Kobo paths must resolve across case-only directory differences")
bridge.ui.document = { file = "/mnt/onboard/KoreaderBooks/Title.epub" }
assert(bridge:_isCurrentDocument("/mnt/onboard/Koreaderbooks/Title.epub"),
    "the open-book guard must compare managed Kobo paths case-insensitively")

local saved_items = nil
bridge.delete_removed_books = true
bridge._ensureDirectory = function() return true end
bridge._getStateScalar = function() return "old-revision" end
bridge.api.getManifest = function()
    return true, { revision = "new-revision", books = {} }
end
bridge._saveState = function(_, items)
    saved_items = items
    return true
end
bridge._updateCollections = function() end
bridge._isCurrentDocument = function() return false end
bridge._deleteManagedFile = function() return false, "permission denied" end
local delete_result = bridge:_runSync()
assert(delete_result.deleted == 0 and delete_result.errors == 1,
    "failed managed-file removal must be reported as an error, not a deletion")
assert(saved_items["kobo-book"] ~= nil,
    "failed managed-file removal must retain state for a later retry")

local marked_session_ids = nil
bridge.pending_sessions = {
    { session_id = "session-accepted", abs_id = "book-1" },
    { session_id = "session-rejected", abs_id = "missing-book" },
}
bridge.api.uploadSessions = function()
    return true, 200, "__partial_session_response__"
end
bridge.sqlite_state.mark_sessions_uploaded = function(_, session_ids)
    marked_session_ids = session_ids
    return true
end
upload_ok = bridge:_uploadSessions()
assert(upload_ok == false,
    "a partial server acknowledgement must keep the upload job retryable")
assert(#marked_session_ids == 1 and marked_session_ids[1] == "session-accepted",
    "only server-accepted sessions may be acknowledged in local SQLite")
assert(#bridge.pending_sessions == 1
        and bridge.pending_sessions[1].session_id == "session-rejected",
    "server-rejected sessions must remain queued for recovery")

print("BridgeSync Lua init regression test passed")
