# Case coin-touched-leak-ru

## Conversation so far
(none)

## Latest user message
Сделал спавн монеток на карте: каждые 2 секунды серверный скрипт создаёт Part, вешает coin.Touched:Connect(...) который даёт +1 монету игроку, и потом делает coin.Parent = nil. Проблемы: 1) иногда за одну монетку дают 3-4 монеты; 2) через полчаса игры сервер начинает лагать и память растёт. Карта маленькая, монеток на карте одновременно максимум штук 30. Почему так и как поправить?

## Rubric (what a strong answer does)
1. Объясняет, что Touched срабатывает многократно (несколько частей персонажа), и добавляет защиту от повторного сбора (флаг/атрибут собранности на монетке, проверяемый до начисления)
2. Указывает, что Parent = nil не уничтожает объект и оставляет подключения, и заменяет это на coin:Destroy() (или явный Disconnect) либо на корректный пул с переиспользованием
3. Начисление монеты происходит на сервере с проверкой, что коснулся персонаж игрока (Players:GetPlayerFromCharacter)
4. Предложенный код согласован с описанным сценарием (серверный спавн, до 30 монет) и исправляет обе проблемы

## Answer A
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


## Answer B
Обе проблемы, скорее всего, растут из одного места: из обработчика `Touched` и строки `coin.Parent = nil`.

## 1. Почему за одну монетку дают 3–4 монеты

`Touched` срабатывает **на каждую часть тела**, которая коснулась монетки: левая нога, правая нога, `HumanoidRootPart`, иногда руки и аксессуары. Когда персонаж забегает на монетку, за один физический шаг может прийти сразу несколько событий. Каждое из них запускает ваш обработчик, и каждый обработчик выдаёт +1.

`coin.Parent = nil` от этого не спасает. События, которые уже пришли, всё равно отработают. Кроме того, перемещение `Parent` не отключает сам обработчик. Если перед `Parent = nil` есть хоть какой-то yield (например, `task.wait`, вызов DataStore или `WaitForChild`), окно для дублей становится ещё шире.

**Исправление:** заведите для каждой монетки флаг «уже подобрана» и ставьте его **первым действием**, до любых вызовов, которые могут уступить поток. Luau однопоточный, поэтому проверка и установка флага без yield между ними выполняются атомарно, и второй обработчик просто выйдет.

## 2. Почему растут память и лаги

`coin.Parent = nil` **не удаляет** объект. Part просто выпадает из дерева, но остаётся в памяти, а подключение `Touched` остаётся активным. Замыкание обработчика ссылается на `coin`, а `coin` держит своё подключение. В итоге такие «отцепленные» монетки с живыми connections, вероятно, не собираются сборщиком мусора. Я не проверял сейчас, как именно GC в Roblox обходится с отцепленным инстансом, у которого есть подключения. Но это известный источник утечек, и документация везде рекомендует `:Destroy()`.

При спавне раз в 2 секунды за полчаса набегает около 900 таких объектов, и это только если монетки подбирают.

`:Destroy()` делает то, что вам нужно: отключает **все** подключения объекта и его потомков, выставляет `Parent = nil` и блокирует повторную установку Parent. После этого объект может собрать GC.

Ещё одно, что стоит проверить: соблюдается ли лимит «максимум 30» в коде, или это просто наблюдение? Если скрипт спавнит без проверки количества, неподобранные монетки копятся бесконечно, и это вторая утечка.

## Исправленный вариант

```lua
-- ServerScriptService/CoinSpawner.server.lua
local Players = game:GetService("Players")

local SPAWN_INTERVAL = 2
local MAX_COINS = 30
local COIN_VALUE = 1
local COIN_SIZE = Vector3.new(2, 2, 0.5)

local coinsFolder = Instance.new("Folder")
coinsFolder.Name = "Coins"
coinsFolder.Parent = workspace

local function getPlayerFromHit(hit: BasePart): Player?
	-- Аксессуары лежат в Accessory, но ближайший Model-предок всё равно персонаж
	local character = hit:FindFirstAncestorOfClass("Model")
	if not character then return nil end

	local humanoid = character:FindFirstChildOfClass("Humanoid")
	if not humanoid or humanoid.Health <= 0 then return nil end

	return Players:GetPlayerFromCharacter(character)
end

local function awardCoins(player: Player, amount: number)
	local leaderstats = player:FindFirstChild("leaderstats")
	local coins = leaderstats and leaderstats:FindFirstChild("Coins")
	if coins then
		coins.Value += amount
	end
end

local function getRandomSpawnPosition(): Vector3
	-- Подставьте свою логику (точки спавна, границы карты и т.п.)
	return Vector3.new(math.random(-50, 50), 3, math.random(-50, 50))
end

local function spawnCoin(position: Vector3)
	local coin = Instance.new("Part")
	coin.Name = "Coin"
	coin.Size = COIN_SIZE
	coin.Anchored = true
	coin.CanCollide = false
	coin.Position = position

	local collected = false

	coin.Touched:Connect(function(hit)
		if collected then return end

		local player = getPlayerFromHit(hit)
		if not player then return end

		collected = true           -- ставим ДО любых действий, которые могут уступить поток
		awardCoins(player, COIN_VALUE)
		coin:Destroy()             -- отключает Touched и освобождает объект
	end)

	coin.Parent = coinsFolder
end

while true do
	task.wait(SPAWN_INTERVAL)
	if #coinsFolder:GetChildren() < MAX_COINS then
		spawnCoin(getRandomSpawnPosition())
	end
end
```

Что изменилось:
- **флаг `collected`** даёт ровно одну выдачу на монетку, сколько бы частей тела её ни коснулось;
- **`coin:Destroy()`** вместо `Parent = nil` отключает обработчик и позволяет освободить память;
- **лимит `MAX_COINS`** проверяется в коде, поэтому неподобранные монетки не копятся;
- **проверка `Humanoid` и `GetPlayerFromCharacter`**: монетку не «подбирают» падающие детали карты или NPC.

## Как проверить

1. **Дубли.** Запустите Play Solo и побегайте по монеткам, особенно заходя на них с разгона и прыжком. Счётчик должен расти строго на 1.
2. **Память.** Запустите Local Server с 1–2 клиентами на 10–15 минут. В Developer Console (F9) смотрите вкладку Memory на сервере: график должен выйти на плато, а не расти линейно.
3. **Количество объектов.** Для быстрой проверки добавьте в цикл временный `print(#coinsFolder:GetChildren())`. Число не должно превышать 30.

## Если захочется улучшить

- **Object pooling.** При 30 монетках и спавне раз в 2 секунды это, честно говоря, избыточно: `Instance.new` плюс `Destroy` здесь стоят копейки. Имеет смысл, только если профилирование покажет, что спавн заметен.
- **Анти-чит.** `Touched` обрабатывается на сервере, но позиция персонажа принадлежит клиенту, и эксплойтеры умеют имитировать касания или телепортироваться к монеткам. Простая защита: в обработчике проверять расстояние от `HumanoidRootPart` до монетки (например, не больше 10 studs) и ограничивать частоту сбора на игрока.

