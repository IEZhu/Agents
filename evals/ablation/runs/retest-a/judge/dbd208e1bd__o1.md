# Case hud-leaderstats-race-en

## Conversation so far
(none)

## Latest user message
My HUD LocalScript in StarterPlayerScripts starts with:

local stats = game.Players.LocalPlayer.leaderstats
local coins = stats.Coins

About 1 in 5 joins I get "leaderstats is not a valid member of Player" and the HUD stays blank. Also on the server I have a loop that every 0.1s does CoinsEvent:FireClient(p, coins), GemsEvent:FireClient(p, gems), XPEvent:FireClient(p, xp) for every player, and the mobile players say the game feels laggy. How do I fix both?

## Rubric (what a strong answer does)
1. Explains the error as a replication/load-order race (leaderstats created by a server script after the LocalScript runs) and fixes it with WaitForChild for leaderstats and its children
2. Recommends combining the three events into one payload and/or sending only when values change (or using .Changed on leaderstats values) instead of firing every 0.1s
3. Notes that values stored in leaderstats (like Coins) already replicate to the client, so the HUD can listen to their .Changed directly instead of receiving them via remotes
4. Answer addresses both problems with correct Luau code

## Answer A
Both problems have the same fix: stop pushing the values to the client yourself, and let the client wait for data that Roblox already replicates.

## 1. "leaderstats is not a valid member of Player": a race condition

Your LocalScript can start running before the server's `PlayerAdded` handler has created `leaderstats` and before that folder has replicated to the client. Indexing with `.leaderstats` fails at once if the child isn't there yet, so the script errors and the HUD never gets set up. Whether it fails depends on timing, which is why you only see it on some joins. If your server loads DataStore data before it creates the folder, the gap is wider and the error happens more often.

**Client fix:** wait for the instances instead of indexing them directly.

```lua
-- StarterPlayerScripts/HUD (LocalScript)
local Players = game:GetService("Players")

local player = Players.LocalPlayer
local leaderstats = player:WaitForChild("leaderstats")
local coins = leaderstats:WaitForChild("Coins")
```

**Server fix:** create the structure right away. Put the children in the folder, parent the folder to the player last, and only then do the slow DataStore load.

```lua
-- ServerScriptService/PlayerStats (Script)
local Players = game:GetService("Players")

local function onPlayerAdded(player: Player)
	local leaderstats = Instance.new("Folder")
	leaderstats.Name = "leaderstats"

	local coins = Instance.new("IntValue")
	coins.Name = "Coins"
	coins.Parent = leaderstats

	-- Parent last so the folder arrives with its children already inside
	leaderstats.Parent = player

	-- Stats you don't want on the leaderboard: attributes replicate too
	player:SetAttribute("Gems", 0)
	player:SetAttribute("XP", 0)

	-- Slow part AFTER the structure exists (wrap your DataStore call in pcall)
	local data = loadPlayerData(player) -- your existing load function
	coins.Value = data.Coins
	player:SetAttribute("Gems", data.Gems)
	player:SetAttribute("XP", data.XP)
end

Players.PlayerAdded:Connect(onPlayerAdded)
-- Covers players who joined before this script connected (common in Studio tests)
for _, player in Players:GetPlayers() do
	task.spawn(onPlayerAdded, player)
end
```

Keep the `WaitForChild("Coins")` on the client even with the server change. It costs nothing and doesn't depend on replication order.

## 2. The lag: you're sending about 30 remote calls per second per player for values that barely change

A 0.1s loop with 3 `FireClient` calls means about 30 messages per second to every player, every second, even when nothing has changed. On mobile, where connections and CPUs are weaker, that constant traffic plus running the HUD handler 30 times a second is a likely cause of the lag you're hearing about.

You don't need those remotes at all. Changes to `IntValue.Value` and to Player attributes already replicate from server to client, and Roblox sends them only when a value actually changes. **Delete the loop** and have the HUD react to changes instead:

```lua
-- Continuing the HUD LocalScript
local hud = player:WaitForChild("PlayerGui"):WaitForChild("HUD") -- adjust to your GUI
local coinsLabel = hud:WaitForChild("CoinsLabel")
local gemsLabel = hud:WaitForChild("GemsLabel")
local xpLabel = hud:WaitForChild("XPLabel")

local function renderCoins()
	coinsLabel.Text = tostring(coins.Value)
end

local function bindAttribute(attributeName: string, label: TextLabel)
	local function render()
		label.Text = tostring(player:GetAttribute(attributeName) or 0)
	end
	player:GetAttributeChangedSignal(attributeName):Connect(render)
	render() -- initial draw: the value may already be set before we connect
end

coins.Changed:Connect(renderCoins)
renderCoins()

bindAttribute("Gems", gemsLabel)
bindAttribute("XP", xpLabel)
```

On the server, just set the values (`coins.Value += 10`, `player:SetAttribute("Gems", gems)`). The client updates only when something changes, which works out to a few updates per minute instead of 1,800.

If you do need a RemoteEvent for these (for example, to play a "+50 coins!" popup), fire it **once at the moment the value changes**, not on a timer.

## Check that it worked

- **Race fix:** in Studio, use the Test tab to start a local server with 2–3 clients, and rejoin several times. The HUD should always fill in. To mimic a slow load, you can temporarily add `task.wait(3)` before `loadPlayerData`. The HUD should wait and then fill in instead of erroring.
- **Lag fix:** open the Developer Console (F9) and compare the network receive rate before and after removing the loop. If mobile players still report lag afterward, the remotes weren't the only cause. Use the MicroProfiler (Ctrl+F6) on a device to find the real bottleneck rather than guessing.

## Security note

Only the server should ever change Coins, Gems or XP. The client should only display them. Don't add a RemoteEvent that lets the client "update" its own stats. An exploiter would call it with whatever number they wanted.


## Answer B
Both problems come from the same root issue. The client and server don't agree on when data exists or how it reaches the client. Here's how to fix each one.

## 1. "leaderstats is not a valid member of Player": a race condition

LocalScripts in StarterPlayerScripts start running almost immediately when the player joins. Your server script creates `leaderstats` in `Players.PlayerAdded`, and that folder has to replicate to the client. Sometimes the client script runs first, and then `LocalPlayer.leaderstats` is `nil` and indexing it throws an error. That's why it only fails some of the time.

The fix is to wait for it with `WaitForChild` instead of indexing it directly:

```lua
-- StarterPlayerScripts/HUD.client.lua
local Players = game:GetService("Players")

local player = Players.LocalPlayer
local leaderstats = player:WaitForChild("leaderstats")
local coins = leaderstats:WaitForChild("Coins")
local gems = leaderstats:WaitForChild("Gems")
local xp = leaderstats:WaitForChild("XP")
```

On the server, also make sure `leaderstats` is created for players who joined before your `PlayerAdded` connection ran. This happens a lot in Studio Play Solo:

```lua
-- ServerScriptService/Leaderstats.server.lua
local Players = game:GetService("Players")

local function setupLeaderstats(player: Player)
    local leaderstats = Instance.new("Folder")
    leaderstats.Name = "leaderstats"

    for _, statName in { "Coins", "Gems", "XP" } do
        local value = Instance.new("IntValue")
        value.Name = statName
        value.Parent = leaderstats
    end

    -- Parent last, so the client never sees a half-built folder
    leaderstats.Parent = player
end

Players.PlayerAdded:Connect(setupLeaderstats)
for _, player in Players:GetPlayers() do
    setupLeaderstats(player)
end
```

## 2. The lag: you're sending data the client already has

Your loop sends 3 events × 10 per second = **30 remote events per second per player**, and most of them carry values that haven't changed. Every one of those events also runs your HUD update code on the client. On mobile, with weaker CPUs and higher-latency networks, that traffic and UI churn is very likely the lag your players feel. I can't be certain it's the only cause without profiling, but it's the obvious first suspect.

The key point is that **`IntValue`s inside `leaderstats` already replicate from server to client automatically.** You don't need `CoinsEvent`, `GemsEvent` or `XPEvent` at all. Delete the loop. The server just sets the values:

```lua
-- Anywhere on the server, when coins are actually earned:
player.leaderstats.Coins.Value += amount
```

The client then reacts only when a value really changes:

```lua
-- HUD LocalScript, continued
local coinsLabel = script.Parent:WaitForChild("CoinsLabel") -- adjust to your UI path

local function renderCoins()
    coinsLabel.Text = tostring(coins.Value)
end

renderCoins()                    -- show the initial value right away
coins.Changed:Connect(renderCoins)
-- repeat for gems and xp
```

That takes you from 30 events per second to **zero traffic while nothing changes**, and one tiny replication update when something does.

### If you do need RemoteEvents (for data that isn't in leaderstats)
- **Fire on change, not on a timer.** Send the update where the value changes, for example in your `AddCoins` function.
- **Batch into one event**, not three: `StatsEvent:FireClient(player, { Coins = c, Gems = g, XP = x })`.
- If values change very often (say, several times per frame from auto-collect), mark the player "dirty" and flush at most every 0.2 to 0.5 s.

## Security note
Keep the server as the only writer. The client should only *read* `leaderstats` for display. Any client change to those values won't replicate anyway. Never accept "I have X coins" from a RemoteEvent. The server calculates rewards itself.

## How to test
1. In Studio, use **Test → Clients and Servers** with 2 or 3 clients and join several times. The leaderstats error should be gone.
2. Open the **Developer Console (F9) → Network** or MicroProfiler before and after. Remote traffic should drop sharply.
3. Use the device emulator (or a real phone) to confirm the HUD updates smoothly when you earn coins.

