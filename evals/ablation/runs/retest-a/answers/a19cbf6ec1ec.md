## What's going on

Your pad gets stuck because `RemoteFunction:InvokeClient` **yields the server thread until the client returns something**, and nothing forces the client to return. If that call never comes back, the line `pad.Busy = false` never runs. The pad stays locked for everyone, and because a yield that never ends isn't an error, nothing shows up in Output.

Ways a real client can leave the server waiting forever:

- **The prompt UI goes away before the player answers.** For example, the player resets or dies and the ScreenGui is destroyed (`ResetOnSpawn` defaults to true), or another script closes the dialog. The client callback is still waiting on a button click that can't happen now, so it never returns.
- **The client-side `OnClientInvoke` isn't set yet**, for example because the LocalScript loads late or errored. As I recall, the invoke then just waits until a callback gets assigned.
- **An exploiter** sets `OnClientInvoke` to a function that yields forever. That lets one person lock a pad on purpose.
- **The player leaves** partway through. I believe the invoke errors in this case, which *would* show in Output, so your silent hangs are more likely one of the cases above. I recalled this from memory and haven't verified it, so check the `RemoteFunction` page on create.roblox.com/docs.

Roblox's own docs warn against `InvokeClient` for this reason. The server ends up depending on the client to finish its own logic. It's also a second problem: `answer` comes from the client, so it can be any value.

## The right pattern: fire an offer, store it on the server, and accept a matching reply

Use two RemoteEvents instead of a RemoteFunction. The server never waits on the client:

1. The pad is touched. The server records a **pending offer** for that player, with an id and an expiry time, and fires the prompt to them.
2. The client shows the UI and fires back `(offerId, accepted)` when the player clicks.
3. The server accepts the reply only if it matches the player's current offer. It then **re-checks everything** (cash, ownership, that the upgrade isn't already bought) and completes the purchase.
4. If the offer expires or the player leaves, the server discards it. Nothing is left blocked.

The lock is now per player instead of per pad, so one player can't block anyone else.

```lua
-- ServerScriptService/UpgradePadService.server.lua
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Remotes = ReplicatedStorage:WaitForChild("Remotes")
local PromptEvent = Remotes:WaitForChild("UpgradePrompt")     -- server -> client
local ResponseEvent = Remotes:WaitForChild("UpgradeResponse") -- client -> server

local OFFER_TIMEOUT_SECONDS = 15

type Offer = { id: number, pad: BasePart, upgradeName: string, cost: number }

local pendingOffers: { [Player]: Offer } = {}
local nextOfferId = 0

local function getCash(player: Player): IntValue?
	local stats = player:FindFirstChild("leaderstats")
	return stats and stats:FindFirstChild("Cash") :: IntValue?
end

local function grantUpgrade(player: Player, pad: BasePart, upgradeName: string)
	-- your existing "spawn Dropper Lv2 / disable pad" logic goes here
	pad:SetAttribute("Purchased", true)
end

local function offerUpgrade(pad: BasePart, hit: BasePart)
	local character = hit:FindFirstAncestorOfClass("Model")
	local player = character and Players:GetPlayerFromCharacter(character)
	if not player then return end
	if pendingOffers[player] then return end              -- already has an open prompt
	if pad:GetAttribute("Purchased") then return end
	if pad:GetAttribute("OwnerUserId") ~= player.UserId then return end -- only the tycoon owner

	-- Price and name come from the server (pad attributes), never from the client
	local upgradeName = pad:GetAttribute("UpgradeName") :: string
	local cost = pad:GetAttribute("Cost") :: number

	nextOfferId += 1
	local offer: Offer = { id = nextOfferId, pad = pad, upgradeName = upgradeName, cost = cost }
	pendingOffers[player] = offer
	PromptEvent:FireClient(player, offer.id, upgradeName, cost)

	-- Expire the offer so an unanswered prompt can't hold anything
	task.delay(OFFER_TIMEOUT_SECONDS, function()
		if pendingOffers[player] == offer then
			pendingOffers[player] = nil
		end
	end)
end

ResponseEvent.OnServerEvent:Connect(function(player: Player, offerId: unknown, accepted: unknown)
	if typeof(offerId) ~= "number" then return end
	local offer = pendingOffers[player]
	if not offer or offer.id ~= offerId then return end  -- stale, forged, or expired
	pendingOffers[player] = nil                           -- consume it: one reply per offer

	if accepted ~= true then return end

	-- Re-validate at purchase time; state may have changed while the prompt was open
	local cash = getCash(player)
	if not cash or cash.Value < offer.cost then return end
	if offer.pad:GetAttribute("Purchased") then return end

	cash.Value -= offer.cost
	grantUpgrade(player, offer.pad, offer.upgradeName)
end)

Players.PlayerRemoving:Connect(function(player)
	pendingOffers[player] = nil
end)

for _, pad in workspace.Tycoons:GetDescendants() do  -- adjust to your layout / CollectionService tag
	if pad:IsA("BasePart") and pad:GetAttribute("UpgradeName") then
		pad.Touched:Connect(function(hit) offerUpgrade(pad, hit) end)
	end
end
```

```lua
-- StarterPlayerScripts/UpgradePromptClient.client.lua
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Remotes = ReplicatedStorage:WaitForChild("Remotes")
local PromptEvent = Remotes:WaitForChild("UpgradePrompt")
local ResponseEvent = Remotes:WaitForChild("UpgradeResponse")

PromptEvent.OnClientEvent:Connect(function(offerId: number, upgradeName: string, cost: number)
	-- Show your dialog: ("Buy %s for %d cash?"):format(upgradeName, cost)
	-- Yes button: ResponseEvent:FireServer(offerId, true)
	-- No button:  ResponseEvent:FireServer(offerId, false)
end)
```

## Why this works

- **Nothing on the server waits on the client.** If the client never answers, the offer expires after 15 seconds and nothing stays locked.
- **The server is the authority.** It sets the price and name, and it checks cash and ownership when the player clicks Yes, not when the prompt opened. The client only sends back "yes" or "no" for an offer id the server created.
- **Each offer id works once.** A fast double-click or a replayed packet can't buy the upgrade twice.
- **It fixes spam from repeated Touched events.** `pendingOffers[player]` blocks new prompts while one is open, so you don't need `pad.Busy`.

## Checking it

In Studio, run a Local Server with 2 players:

1. Step on the pad and then reset your character without answering. The pad should work again within 15 seconds, and it should never lock for the other player.
2. Temporarily comment out the client's `FireServer` calls so the client never replies. The server should keep working.
3. Click Yes with too little cash. Nothing should be deducted.

## If you want to keep `InvokeClient`

You'd need to run it in a separate thread with a timeout and still validate the return value. Even then, a client that never answers leaves a thread waiting forever, and the code ends up more complicated than the event version. I'd switch to the two-event pattern.
