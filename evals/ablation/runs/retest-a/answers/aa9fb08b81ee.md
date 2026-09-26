Обе проблемы типичные, и у каждой есть конкретная причина.

## 1) Почему за одну монетку дают 3-4 монеты

`Touched` срабатывает не один раз на касание персонажа, а **для каждой части тела**, которая коснулась монетки: левая нога, правая нога, `HumanoidRootPart`, торс и так далее. За один кадр прилетает несколько событий подряд. Обработчик каждый раз находит игрока и начисляет +1. К тому же `coin.Parent = nil` не отключает уже поставленные в очередь срабатывания.

Значит, нужен флаг «уже собрана», и проверять его надо **первой строкой** обработчика, до любых `yield`.

## 2) Почему растут лаги и память

`coin.Parent = nil` только убирает объект из мира, но **не уничтожает его**:
- соединение `Touched` остаётся живым;
- функция-обработчик хранит ссылку на `coin` в замыкании, а соединение хранит функцию. Получается цикл ссылок, и сборщик мусора часто не может освободить такой инстанс.

При спавне каждые 2 секунды за 30 минут набирается около 900 «призрачных» Part с живыми соединениями, и так без конца. Проблема не в том, сколько монеток лежит на карте одновременно, а в том, что собранные не удаляются.

`coin:Destroy()` отключает все соединения инстанса и его потомков и блокирует `Parent`. После этого объект может собрать GC. Правило простое: если объект больше не нужен, вызывайте `Destroy()`, а не `Parent = nil`.

## Исправленный вариант

```lua
-- ServerScriptService/CoinSpawner.server.lua
local Players = game:GetService("Players")

local SPAWN_INTERVAL = 2
local MAX_COINS = 30
local COIN_VALUE = 1

local coinsFolder = Instance.new("Folder")
coinsFolder.Name = "Coins"
coinsFolder.Parent = workspace

local function getRandomSpawnPosition(): Vector3
	-- подставьте свою логику (точки спавна, границы карты)
	return Vector3.new(math.random(-50, 50), 3, math.random(-50, 50))
end

local function awardCoin(player: Player)
	local leaderstats = player:FindFirstChild("leaderstats")
	local coins = leaderstats and leaderstats:FindFirstChild("Coins")
	if coins then
		coins.Value += COIN_VALUE
	end
end

local function spawnCoin()
	local coin = Instance.new("Part")
	coin.Name = "Coin"
	coin.Shape = Enum.PartType.Cylinder
	coin.Size = Vector3.new(0.4, 2, 2)
	coin.Anchored = true
	coin.CanCollide = false
	coin.Position = getRandomSpawnPosition()

	local collected = false

	coin.Touched:Connect(function(hit)
		if collected then return end -- защита от повторных срабатываний

		local character = hit.Parent
		local player = Players:GetPlayerFromCharacter(character)
		if not player then return end

		local humanoid = character:FindFirstChildOfClass("Humanoid")
		if not humanoid or humanoid.Health <= 0 then return end

		collected = true -- ставим ДО любого yield
		awardCoin(player)
		coin:Destroy() -- отключает соединение и освобождает память
	end)

	coin.Parent = coinsFolder
end

while true do
	task.wait(SPAWN_INTERVAL)
	if #coinsFolder:GetChildren() < MAX_COINS then
		spawnCoin()
	end
end
```

Что поменялось:
- **`collected`**: локальный флаг на каждую монетку. Второе и последующие срабатывания `Touched` сразу выходят. Флаг ставится до начисления, поэтому гонки между событиями нет.
- **`Players:GetPlayerFromCharacter`**: засчитываются только настоящие персонажи игроков, а не случайные Part или NPC.
- **`Destroy()` вместо `Parent = nil`**: утечка памяти уходит.
- **`MAX_COINS` в коде**: лимит теперь соблюдает сам скрипт. Если лимита не было, монетки, которые никто не собирает, тоже копились бы.
- **Всё на сервере**: клиент ничего не решает, поэтому эксплойтер не может просто вызвать RemoteEvent «дай монетку». Учтите, что `Touched` от персонажа всё равно основан на физике, которую частично считает клиент. Если игра станет серьёзной, добавьте проверку расстояния, например `(root.Position - coin.Position).Magnitude < 10`, чтобы телепорт-читы не собирали монеты по всей карте.

## Как проверить

1. **Дубли.** Запустите Play Solo и пробегите через монетку. Счётчик должен вырасти ровно на 1. Для уверенности временно добавьте `print("collected by", player.Name)` в обработчик: строка должна появляться один раз на монетку.
2. **Память.** Откройте Developer Console (F9), вкладка Memory, и следите за счётчиком инстансов и серверной памятью Lua в течение 10-15 минут игры. Со старым кодом графики растут линейно, с новым выходят на плато. Точные названия пунктов во вкладке Memory я привожу по памяти, у вас они могут немного отличаться.
3. **Нагрузочный тест.** Временно поставьте `SPAWN_INTERVAL = 0.1` и собирайте монетки. Так утечка, если она осталась, проявится за минуту, а не за полчаса.

## На будущее

- Если монеток станет сотни и они будут спавниться очень часто, можно перейти на **object pooling**: не уничтожать монетку, а прятать её и переиспользовать. При 30 штуках раз в 2 секунды это избыточно, `Destroy()` вполне достаточно.
- Чтобы монетка исчезала на всех клиентах мгновенно, а не с задержкой репликации, можно сначала сделать `coin.Transparency = 1` и потом `Destroy()`. Но обычно разница незаметна.
