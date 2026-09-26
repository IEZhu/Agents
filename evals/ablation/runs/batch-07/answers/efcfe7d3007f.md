Обе проблемы вызваны одним и тем же: **сервер хранит баланс только в DataStore и работает с ним напрямую при каждой покупке**. Если держать баланс в памяти сервера, то есть в `leaderstats`, и сохранять его в DataStore только при выходе игрока и периодически, исправятся оба бага.

## Почему цифра не обновляется

GUI читает `leaderstats.Coins`, а покупка меняет только запись в DataStore. DataStore не реплицируется на клиента и ничего не знает про `leaderstats`. Поэтому значение в `leaderstats` остаётся старым, пока при следующем входе его не загрузят заново из DataStore. К тому же GUI читает значение один раз при старте и не подписан на изменения.

## Почему выдаёт два меча (и это не «редкий баг», а дыра)

`GetAsync` — это сетевой запрос, и он **yield'ит** поток. Если кликнуть дважды быстро, получится так:

1. Обработчик №1: `GetAsync` → ждёт ответа...
2. Обработчик №2: `GetAsync` → тоже ждёт...
3. Оба получают `100`, оба проходят проверку `100 >= price`.
4. Оба пишут `100 - price` → **монеты списаны один раз, мечей выдано два**.

Это классический race condition между проверкой и списанием. Обычные игроки ловят его случайно, а эксплойтер может вызвать `BuyItem:FireServer("Sword")` 50 раз в цикле и получить кучу предметов по цене одного. Кроме того, у DataStore есть лимиты запросов на сервер. Точные цифры я привожу по памяти, не проверял: примерно `60 + 10 × игроков` Get/Set в минуту, сверьтесь с документацией. На 40 игроках частые покупки через DataStore начнут упираться в троттлинг.

Есть и другие проблемы в текущем коде:
- `prices[itemName]` упадёт с ошибкой, если клиент пришлёт несуществующее имя или не строку. Любой RemoteEvent-аргумент от клиента нужно проверять.
- Для нового игрока `GetAsync` вернёт `nil`, и `nil >= price` тоже выбросит ошибку.
- Вызовы `GetAsync`/`SetAsync` не обёрнуты в `pcall`, поэтому при сбое DataStore скрипт падает.

## Исправленный вариант

### Серверный Script (ServerScriptService)

Если `leaderstats` у вас уже создаётся в другом скрипте, объедините логику в одном месте. Важно, чтобы источник правды был один: все, кто выдаёт или списывает монеты (подбор монет, награды и т.д.), должны менять `Coins.Value`, а не писать в DataStore напрямую.

```lua
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local ServerStorage = game:GetService("ServerStorage")
local DataStoreService = game:GetService("DataStoreService")

local coinStore = DataStoreService:GetDataStore("Coins")
local Prices = ReplicatedStorage.Prices
local Items = ServerStorage.Items
local BuyItem = ReplicatedStorage.BuyItem

local STARTING_COINS = 0
local AUTOSAVE_INTERVAL = 120 -- секунд

-- true только если данные игрока успешно загружены.
-- Без этого при сбое GetAsync мы бы сохранили 0 поверх реального баланса.
local loaded: {[Player]: boolean} = {}

local function getCoins(player: Player): IntValue?
	local leaderstats = player:FindFirstChild("leaderstats")
	return leaderstats and leaderstats:FindFirstChild("Coins") :: IntValue?
end

local function savePlayer(player: Player)
	if not loaded[player] then return end
	local coins = getCoins(player)
	if not coins then return end

	local value = coins.Value
	local ok, err = pcall(function()
		coinStore:SetAsync(player.UserId, value)
	end)
	if not ok then
		warn(("Не удалось сохранить монеты %s: %s"):format(player.Name, err))
	end
end

Players.PlayerAdded:Connect(function(player)
	local leaderstats = Instance.new("Folder")
	leaderstats.Name = "leaderstats"

	local coins = Instance.new("IntValue")
	coins.Name = "Coins"
	coins.Parent = leaderstats
	leaderstats.Parent = player

	local ok, saved = pcall(function()
		return coinStore:GetAsync(player.UserId)
	end)
	if not ok then
		warn(("Не удалось загрузить монеты %s: %s"):format(player.Name, saved))
		return -- loaded не ставим: покупки и сохранение заблокированы
	end

	coins.Value = saved or STARTING_COINS
	loaded[player] = true
end)

Players.PlayerRemoving:Connect(function(player)
	savePlayer(player)
	loaded[player] = nil
end)

-- Автосейв, чтобы не терять прогресс при краше сервера
task.spawn(function()
	while true do
		task.wait(AUTOSAVE_INTERVAL)
		for _, player in Players:GetPlayers() do
			task.spawn(savePlayer, player)
		end
	end
end)

-- Сохранение при выключении сервера
game:BindToClose(function()
	local pending = 0
	for _, player in Players:GetPlayers() do
		pending += 1
		task.spawn(function()
			savePlayer(player)
			pending -= 1
		end)
	end
	while pending > 0 do
		task.wait()
	end
end)

BuyItem.OnServerEvent:Connect(function(player, itemName)
	-- 1. Валидация входных данных: клиенту не доверяем
	if typeof(itemName) ~= "string" then return end
	if not loaded[player] then return end

	local priceValue = Prices:FindFirstChild(itemName)
	local itemTemplate = Items:FindFirstChild(itemName)
	if not (priceValue and priceValue:IsA("IntValue") and itemTemplate) then return end

	local coins = getCoins(player)
	local backpack = player:FindFirstChildOfClass("Backpack")
	if not (coins and backpack) then return end

	-- 2. Проверка и списание БЕЗ yield между ними.
	-- Здесь нет ни одного вызова, который отдаёт управление, поэтому второй
	-- клик физически не может вклиниться между проверкой и списанием.
	local price = priceValue.Value
	if coins.Value < price then return end

	coins.Value -= price
	itemTemplate:Clone().Parent = backpack
end)
```

Ключевой момент — второй пункт в обработчике. Скрипты в Roblox выполняются по очереди, и обработчик не прерывается, пока сам не сделает yield (`GetAsync`, `task.wait`, `WaitForChild` и т.п.). Раньше `GetAsync` стоял между проверкой и записью, теперь там нет ни одного yield, поэтому race condition исчезает без всяких debounce-флагов. Если позже добавите в этот участок что-то yield'ящее, например запись покупки в DataStore, понадобится блокировка на игрока.

Отдельный вопрос: может ли игрок купить **два одинаковых** меча честно, если у него хватает денег? Сейчас может. Если меч должен быть в одном экземпляре, добавьте перед списанием проверку `backpack:FindFirstChild(itemName)` и `player.Character:FindFirstChild(itemName)` (надетый меч лежит в персонаже, а не в рюкзаке).

### LocalScript для GUI с монетами

```lua
local Players = game:GetService("Players")

local player = Players.LocalPlayer
local coins = player:WaitForChild("leaderstats"):WaitForChild("Coins")
local label = script.Parent -- TextLabel

local function refresh()
	label.Text = tostring(coins.Value)
end

refresh()
coins.Changed:Connect(refresh)
```

`IntValue` реплицируется с сервера на клиента, и `Changed` сработает сразу после списания. Цифра будет меняться после покупки, при подборе монет, в общем при любом изменении.

LocalScript в кнопке можно оставить как есть. Если хотите, добавьте клиентский кулдаун ради UX, чтобы не спамить сервер, но **защитой он не является**: эксплойтер вызовет remote в обход кнопки.

## Как проверить

1. **Обновление GUI:** Play в Studio, купить предмет, цифра должна измениться сразу.
2. **Дубликаты:** Test → Local Server с двумя клиентами. Дайте игроку денег ровно на один меч и кликайте очень быстро (или временно поставьте в LocalScript `for i = 1, 20 do rs.BuyItem:FireServer(...) end`). Должен выдаться ровно один меч.
3. **Плохой ввод:** в том же тестовом скрипте отправьте `FireServer(nil)`, `FireServer(123)`, `FireServer("НетТакого")`. В Output не должно быть ошибок сервера.
4. **Сохранение:** для DataStore в Studio включите Game Settings → Security → Enable Studio Access to API Services. Купите предмет, выйдите, зайдите снова: баланс должен совпасть.

## Дальше

- `SetAsync` при выходе нормально подходит для старта. Но если игрок быстро перезаходит на другой сервер, старый сервер может перезаписать данные уже после того, как новый их загрузил. Для симулятора, где экономика — основа игры, я бы со временем перешёл на `UpdateAsync` с session locking или готовую библиотеку. Раньше это был ProfileService, у его автора, насколько я помню, есть более новый ProfileStore; актуальный статус проверьте на DevForum.
- Предметы в `Backpack` пропадают при смерти/респавне. Если мечи должны оставаться, храните список купленного в данных игрока и выдавайте заново при `CharacterAdded` (или кладите копию в `StarterGear`).
