--[[--
Resumable full-library highlight sweep.

History discovery is chunked by a small CPU budget, and each book exchange is
scheduled separately. The persisted index advances only after the exchange and
ack succeed, so cancellation, suspend, or network failure resumes safely.
]]

local DocSettings = require("docsettings")
local Trapper = require("ui/trapper")
local UIManager = require("ui/uimanager")
local lfs = require("libs/libkoreader-lfs")

local BridgeAnnotations = require("bridge_annotations")

local STATE_KEY = "annotation_sweep"
local CHUNK_BUDGET_SECONDS = 0.02

local BridgeSweep = {
    running = false,
    cancel_requested = false,
    generation = 0,
}

function BridgeSweep.isRunning()
    return BridgeSweep.running
end

function BridgeSweep.cancel()
    BridgeSweep.cancel_requested = true
end

local function currentReaderFile()
    local ok, ReaderUI = pcall(require, "apps/reader/readerui")
    if ok and ReaderUI and ReaderUI.instance and ReaderUI.instance.document then
        return ReaderUI.instance.document.file
    end
    return nil
end

local function sweepOneBook(bridge, file)
    if file == currentReaderFile() or lfs.attributes(file, "mode") ~= "file" then
        return { skipped = true }
    end

    local doc_settings = DocSettings:open(file)
    local hash = BridgeAnnotations.resolveBookHash(file, doc_settings)
    if not hash then return { skipped = true } end

    local result, err
    local ok_call, pcall_err = pcall(function()
        result, err = BridgeAnnotations.exchangeBooks(bridge, {
            {
                file = file,
                hash = hash,
                annotations = doc_settings:readSetting("annotations") or {},
                live = false,
            },
        }, { keys_complete = false, ignore_watermark = true })
    end)
    if not ok_call then return nil, tostring(pcall_err) end
    if not result then return nil, tostring(err or "exchange failed") end
    return result
end

function BridgeSweep.start(bridge, on_progress, on_done)
    if BridgeSweep.running then return false, "already running" end

    BridgeSweep.running = true
    BridgeSweep.cancel_requested = false
    BridgeSweep.generation = BridgeSweep.generation + 1
    local generation = BridgeSweep.generation
    local totals = { books = 0, skipped = 0, uploaded = 0, applied = 0, deleted = 0 }

    local function persist(queue, index)
        bridge.state:saveSetting(STATE_KEY, { queue = queue, index = index })
        bridge.state:flush()
    end

    local function finish(message, keep_state)
        if generation ~= BridgeSweep.generation then return end
        BridgeSweep.running = false
        BridgeSweep.generation = BridgeSweep.generation + 1
        if not keep_state then
            bridge.state:delSetting(STATE_KEY)
            bridge.state:flush()
        end
        if on_done then on_done(totals, message) end
    end

    local function cancelled(queue, index)
        if not BridgeSweep.cancel_requested then return false end
        if queue and #queue > 0 then persist(queue, index or 1) end
        local resumable = queue and #queue > 0
        finish(resumable and "cancelled - will resume from here" or "cancelled", resumable)
        return true
    end

    local function runQueue(queue, index)
        persist(queue, index)

        local step
        step = function(i)
            if generation ~= BridgeSweep.generation or cancelled(queue, i) then return end
            if i > #queue then
                finish(nil, false)
                return
            end

            Trapper:wrap(function()
                if generation ~= BridgeSweep.generation or cancelled(queue, i) then return end
                local result, err = sweepOneBook(bridge, queue[i])
                if result then
                    if result.skipped then
                        totals.skipped = totals.skipped + 1
                    else
                        totals.books = totals.books + 1
                        totals.uploaded = totals.uploaded + (result.uploaded or 0)
                        totals.applied = totals.applied + (result.applied or 0)
                        totals.deleted = totals.deleted + (result.deleted or 0)
                    end
                    persist(queue, i + 1)
                    if on_progress then on_progress(i, #queue) end
                    UIManager:scheduleIn(0, function()
                        if generation == BridgeSweep.generation then step(i + 1) end
                    end)
                else
                    bridge:logWarn("Highlight sweep stopped at", tostring(queue[i]), ":", tostring(err))
                    persist(queue, i)
                    finish("stopped: " .. tostring(err) .. " - will resume from here", true)
                end
            end)
        end

        UIManager:scheduleIn(0, function()
            if generation == BridgeSweep.generation then step(index) end
        end)
    end

    local saved = bridge.state:readSetting(STATE_KEY)
    if type(saved) == "table" and type(saved.queue) == "table"
        and #saved.queue > 0 and tonumber(saved.index) then
        local index = tonumber(saved.index)
        if index <= #saved.queue then
            bridge:logInfo("Highlight sweep: resuming at book", tostring(index), "of", tostring(#saved.queue))
            runQueue(saved.queue, index)
            return true
        end
    end

    local ok_hist, ReadHistory = pcall(require, "readhistory")
    local history = (ok_hist and ReadHistory and ReadHistory.hist) or {}
    local queue, seen, history_index = {}, {}, 1

    local buildChunk
    buildChunk = function()
        if generation ~= BridgeSweep.generation or cancelled(nil, nil) then return end
        local started = os.clock()
        repeat
            local item = history[history_index]
            local file = type(item) == "table" and item.file or nil
            if type(file) == "string" and file ~= "" and not seen[file]
                and lfs.attributes(file, "mode") == "file"
                and DocSettings:hasSidecarFile(file) then
                seen[file] = true
                queue[#queue + 1] = file
            end
            history_index = history_index + 1
        until history_index > #history or os.clock() - started >= CHUNK_BUDGET_SECONDS

        if history_index <= #history then
            UIManager:scheduleIn(0, function()
                if generation == BridgeSweep.generation then buildChunk() end
            end)
        elseif #queue == 0 then
            finish("no books with highlights sidecars in history", false)
        else
            runQueue(queue, 1)
        end
    end

    UIManager:scheduleIn(0, buildChunk)
    return true
end

return BridgeSweep
