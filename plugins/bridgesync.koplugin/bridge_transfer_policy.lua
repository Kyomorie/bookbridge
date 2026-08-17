local STALL_TIMEOUT = 30
local BASE_TIMEOUT = 120
local MIN_BYTES_PER_SECOND = 8 * 1024
local MAX_TIMEOUT = 6 * 60 * 60
local MAX_TRANSFER_BYTES = 512 * 1024 * 1024
local SIZE_MARGIN = 1.25
local SIZE_SLACK_BYTES = 1024 * 1024

local TransferPolicy = {
    MAX_TRANSFER_BYTES = MAX_TRANSFER_BYTES,
}

function TransferPolicy.timeouts(expected_bytes)
    local size = math.max(0, tonumber(expected_bytes) or 0)
    if size == 0 then return STALL_TIMEOUT, 300 end
    return STALL_TIMEOUT,
        math.min(BASE_TIMEOUT + math.ceil(size / MIN_BYTES_PER_SECOND), MAX_TIMEOUT)
end

function TransferPolicy.maxBytes(expected_bytes)
    local size = tonumber(expected_bytes)
    if not size or size <= 0 then return MAX_TRANSFER_BYTES end
    return math.min(MAX_TRANSFER_BYTES, math.ceil(size * SIZE_MARGIN) + SIZE_SLACK_BYTES)
end

return TransferPolicy
