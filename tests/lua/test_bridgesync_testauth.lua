-- Test Connection must prove the server is BookBridge, not merely that it
-- speaks KoSync. bridge_api_client is the only plugin module under test here,
-- so it gets its own harness: test_bridgesync_core.lua makes json.decode raise
-- (scalar tests must never reach it) and test_bridgesync_init.lua replaces
-- bridge_api_client with a fake.
local plugin_dir = assert(arg[1], "plugin directory argument required")
package.path = plugin_dir .. "/?.lua;" .. package.path

package.preload["logger"] = function()
    return { info = function() end, warn = function() end, err = function() end }
end
package.preload["socket"] = function()
    return {
        skip = function(count, ...)
            return select(count + 1, ...)
        end,
        sleep = function() end,
    }
end
package.preload["ltn12"] = function()
    return { source = { string = function(value) return value end } }
end
package.preload["socketutil"] = function()
    return {
        TIMEOUT_CODE = -1,
        SSL_HANDSHAKE_CODE = -2,
        SINK_TIMEOUT_CODE = -3,
        set_timeout = function() end,
        reset_timeout = function() end,
        table_sink = function(target)
            return function(chunk)
                if chunk then target[#target + 1] = chunk end
                return 1
            end
        end,
    }
end

-- Per-endpoint HTTP responses, reassigned between cases. Matching on a URL
-- substring is what lets one client see a healthy auth route and a hostile
-- device-sync route in the same run.
local http_routes = {}
package.preload["socket.http"] = function()
    return {
        request = function(request)
            local route
            for substring, candidate in pairs(http_routes) do
                if request.url:find(substring, 1, true) then
                    route = candidate
                    break
                end
            end
            route = route or { status = 200, body = "" }
            request.sink(route.body or "")
            request.sink(nil)
            -- Headers must be non-nil: the client reads a nil header table as a
            -- connection failure and retries instead of honouring the status.
            return 1, route.status, {}, route.status_text or "OK"
        end,
    }
end

-- Only two flat payloads need decoding, so a pattern-based reader beats pulling
-- in a JSON library.
package.preload["json"] = function()
    return {
        encode = function() error("json.encode must not be reached by testAuth tests") end,
        decode = function(str)
            local result = {}
            result.name = (str or ""):match('"name"%s*:%s*"([^"]+)"')
            result.version = (str or ""):match('"version"%s*:%s*"([^"]+)"')
            result.message = (str or ""):match('"message"%s*:%s*"([^"]+)"')
            return result
        end,
    }
end

local APIClient = require("bridge_api_client")

local function client()
    local api = APIClient:new()
    api:init("http://bridge", "reader", "secret", nil, function(task)
        return true, task()
    end)
    return api
end

-- A server that answers the BookBridge-only identity probe is reported as
-- BookBridge, and the version it reported reaches the user.
http_routes = {
    ["/koreader/users/auth"] = { status = 200, body = "ok" },
    ["/koreader/device-sync/plugin/version"] = {
        status = 200,
        body = '{"name":"bridgesync","version":"9.9.9"}',
    },
}
local ok, message = client():testAuth()
assert(ok, "a server answering the device-sync identity probe must pass Test Connection")
assert(message:find("BookBridge", 1, true),
    "a confirmed server must be named as BookBridge in the result")
assert(message:find("9.9.9", 1, true),
    "the plugin version reported by the server must reach the user")

-- Issue #403: BridgeSync pointed at Grimmory passed Test Connection and then
-- failed on every real operation with Spring Boot's 405. Any KoSync server
-- accepts /users/auth, so authenticating alone must not be reported as success.
http_routes = {
    ["/koreader/users/auth"] = { status = 200, body = "ok" },
    ["/koreader/device-sync/plugin/version"] = {
        status = 405,
        body = '{"message":"Method \'POST\' is not supported.","status":405}',
        status_text = "Method Not Allowed",
    },
}
ok, message = client():testAuth()
assert(not ok,
    "a KoSync server without the device-sync API must fail Test Connection")
assert(message:find("did not respond as BookBridge", 1, true),
    "the failure must tell the user the server is not BookBridge")

-- The pre-existing authentication failure path keeps its own wording.
http_routes = {
    ["/koreader/users/auth"] = { status = 401, body = "Unauthorized" },
}
ok, message = client():testAuth()
assert(not ok, "rejected credentials must still fail Test Connection")
assert(message:match("^Auth failed:"),
    "a credential rejection must keep reporting itself as an auth failure")

print("BridgeSync Lua testAuth regression tests passed")
