# Case pegboard-slow-loop-thin-wall-ru

## Conversation so far
(none)

## Latest user message
Делаю скриптом в Blender 4.1 органайзер-перфопанель под печать на Ender 3 (сопло 0.4, PLA). Панель 200x150 мм, толщина 3 мм, по сетке с шагом 8 мм торчат штырьки диаметром 3 мм и высотой 12 мм, это примерно 450 штук. Сделал так:

```python
for i in range(cols):
    for j in range(rows):
        bpy.ops.mesh.primitive_cylinder_add(radius=1.5, depth=12, location=(i*8, j*8, 7.5))
        pegs.append(bpy.context.active_object)
# потом выделяю панель и все штырьки и делаю bpy.ops.object.join()
```

Скрипт отрабатывает минуты три, иногда Blender вообще подвисает. Ещё хочу, чтобы штырьки были полыми внутри (экономия пластика): внешний радиус 1.5, внутренний 1.1 мм. Как это ускорить и сделать полые штырьки?

## Rubric (what a strong answer does)
1. Replaces the per-peg bpy.ops calls with a faster approach (e.g. building all pegs in one bmesh / low-level mesh data, or duplicating mesh data without operators) and explains that operator calls in a loop are the bottleneck
2. Points out that a 1.5/1.1 mm radius hollow peg gives a 0.4 mm wall, which is too thin for FDM (below the ~1.2 mm minimum), and proposes keeping the pegs solid or changing the dimensions
3. Notes that join() leaves the pegs and panel as separate intersecting shells (internal faces inside the solid, non-manifold result) and recommends fusing them (boolean union or building connected geometry) so the exported mesh is a single watertight solid
4. The proposed code is plausible Blender Python for 4.x and keeps dimensions as parameters rather than hard-coding them in the loop

## Answer A
## Почему медленно

Сам цилиндр создаётся быстро. Время уходит на то, что вокруг `bpy.ops.mesh.primitive_cylinder_add`. Каждый вызов создаёт новый объект и новый mesh-datablock, добавляет шаг в undo и запускает обновление depsgraph. Имя проверяется на уникальность среди всё большего числа объектов, поэтому каждый следующий вызов дороже предыдущего. Потом `join()` сливает 450 datablock'ов в один. Отсюда минуты и подвисания.

Решение: ни одного `bpy.ops` в цикле. Все штырьки строятся в **одном** `bmesh` из вершин и граней, и получается один объект. На ~27 тыс. граней это должно занимать доли секунды. Замер не делал, но разница с текущим вариантом будет на порядки.

## Ещё две проблемы в текущем коде

1. **Высота штырька.** Цилиндр `depth=12` с центром в `z=7.5` занимает по высоте от 1.5 до 13.5 мм.
   - Если панель лежит в 0…3, штырёк уходит в неё на 1.5 мм и торчит только на **10.5 мм**, а не на 12.
   - Если панель стоит по умолчанию по центру (−1.5…1.5), штырёк лишь касается её грани. Тогда это два отдельных тела с общей плоскостью, и слайсер может «отклеить» штырьки.
2. **`join()` — это не объединение.** Меши просто оказываются в одном объекте, а их объёмы пересекаются друг с другом. PrusaSlicer и Cura обычно такие пересекающиеся оболочки сливают, но настоящей манифолд-геометрии это не даёт. 3D Print Toolbox покажет intersections. Ниже я делаю один Boolean Union (EXACT) между панелью и *одним* объектом со всеми штырьками. Это одна операция, а не 450.

## Про полые штырьки: сначала предупреждение

При внешнем радиусе 1.5 и внутреннем 1.1 стенка получается **0.4 мм, то есть ровно одна линия сопла 0.4**. Последствия:

- Трубка диаметром 3 мм и высотой 12 мм в одну линию слабая. Штырьки на перфопанели работают на изгиб, а FDM-детали и так ломаются по слоям. Однопериметровая стенка, скорее всего, будет трескаться при навешивании инструмента.
- Как слайсер обработает стенку 0.4 мм, зависит от настроек. Arachne в Cura или PrusaSlicer 2.6+ напечатает её одной линией. Classic-генератор периметров может её частично потерять.
- Экономия есть, но скромная. Полость одного штырька: π·1.1²·12 ≈ 45.6 мм³. На 432 штырька это ≈ 19.7 см³, то есть **около 24 г PLA**.

Рекомендация: печатать штырьки **сплошными**. Их сплошное сечение на ~90% и так состоит из двух периметров. Если пластик важен, компромисс такой: стенка ≥ 0.8 мм (`peg_inner_radius = 0.7`), это два периметра, экономия ≈ 9 г. Вариант 1.5/1.1 скрипт тоже построит, но выведет предупреждение.

## Скрипт (Blender 4.1)

📋 Параметры, которые я предположил:
- Панель лежит в 0…200 × 0…150 × 0…3 мм. Сетка центрирована, отступ от края ≥ 5 мм. Получается 24 × 18 = 432 штырька (у вас «около 450»).
- Штырёк выступает на 12 мм над панелью и уходит в неё на 0.5 мм, чтобы Boolean-объединение было чистым.
- Полость открыта сверху. Её дно совпадает с верхней плоскостью панели, потому что после объединения часть полости внутри панели заполняется.

```python
import math

import bpy
import bmesh
from mathutils import Matrix

PARAMS = {
    "panel_width": 200.0,      # mm, X
    "panel_depth": 150.0,      # mm, Y
    "panel_thickness": 3.0,    # mm, Z
    "pitch": 8.0,              # mm, grid step
    "min_margin": 5.0,         # mm, min distance from panel edge to peg center
    "peg_outer_radius": 1.5,   # mm
    "peg_inner_radius": 1.1,   # mm, 0.0 = solid peg; 0.7 recommended for 2 perimeters
    "peg_height": 12.0,        # mm, above panel top surface
    "peg_embed": 0.5,          # mm, overlap into panel so the union is clean
    "peg_segments": 16,        # facets per peg circle
    "nozzle_diameter": 0.4,    # mm, used only for the wall-thickness warning
    "boolean_union": True,     # False = overlapping shells (faster, slicer merges them)
}


def clean_scene() -> None:
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for col in bpy.data.collections:
        if col.name != "Collection":
            bpy.data.collections.remove(col)


def setup_print_units() -> None:
    bpy.context.scene.unit_settings.system = 'METRIC'
    bpy.context.scene.unit_settings.scale_length = 0.001
    bpy.context.scene.unit_settings.length_unit = 'MILLIMETERS'


def check_params(params: dict) -> None:
    r_out = params["peg_outer_radius"]
    r_in = params["peg_inner_radius"]
    if not 0.0 <= r_in < r_out:
        raise ValueError(f"peg_inner_radius must be in [0, {r_out}), got {r_in}")
    if params["pitch"] <= 2 * r_out:
        raise ValueError(f"pitch {params['pitch']} must exceed peg diameter {2 * r_out}")
    wall = r_out - r_in
    min_wall = 2 * params["nozzle_diameter"]
    if r_in > 0 and wall < min_wall:
        print(f"WARNING: peg wall {wall:.2f} mm < {min_wall:.2f} mm (2 perimeters) - pegs will be fragile")


def create_mesh_object(name: str) -> tuple[bpy.types.Object, bmesh.types.BMesh]:
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj, bmesh.new()


def finalize_bmesh(obj: bpy.types.Object, bm: bmesh.types.BMesh) -> None:
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def grid_positions(params: dict) -> list[tuple[float, float]]:
    pitch = params["pitch"]
    cols = int((params["panel_width"] - 2 * params["min_margin"]) // pitch) + 1
    rows = int((params["panel_depth"] - 2 * params["min_margin"]) // pitch) + 1
    x0 = (params["panel_width"] - (cols - 1) * pitch) / 2
    y0 = (params["panel_depth"] - (rows - 1) * pitch) / 2
    return [(x0 + i * pitch, y0 + j * pitch) for i in range(cols) for j in range(rows)]


def add_panel(bm: bmesh.types.BMesh, params: dict) -> None:
    w, d, t = params["panel_width"], params["panel_depth"], params["panel_thickness"]
    matrix = Matrix.Translation((w / 2, d / 2, t / 2)) @ Matrix.Diagonal((w, d, t, 1.0))
    bmesh.ops.create_cube(bm, size=1.0, matrix=matrix)


def unit_circle(segments: int) -> list[tuple[float, float]]:
    return [(math.cos(2 * math.pi * k / segments), math.sin(2 * math.pi * k / segments))
            for k in range(segments)]


def add_ring(bm: bmesh.types.BMesh, cx: float, cy: float, z: float,
             radius: float, circle: list[tuple[float, float]]) -> list[bmesh.types.BMVert]:
    return [bm.verts.new((cx + radius * c, cy + radius * s, z)) for c, s in circle]


def add_solid_peg(bm: bmesh.types.BMesh, cx: float, cy: float, z0: float, z1: float,
                  radius: float, circle: list[tuple[float, float]]) -> None:
    bottom = add_ring(bm, cx, cy, z0, radius, circle)
    top = add_ring(bm, cx, cy, z1, radius, circle)
    n = len(circle)
    for k in range(n):
        k1 = (k + 1) % n
        bm.faces.new((bottom[k], bottom[k1], top[k1], top[k]))
    bm.faces.new(top)
    bm.faces.new(list(reversed(bottom)))


def add_hollow_peg(bm: bmesh.types.BMesh, cx: float, cy: float, z0: float, z1: float,
                   r_out: float, r_in: float, circle: list[tuple[float, float]]) -> None:
    ob = add_ring(bm, cx, cy, z0, r_out, circle)
    ot = add_ring(bm, cx, cy, z1, r_out, circle)
    ib = add_ring(bm, cx, cy, z0, r_in, circle)
    it = add_ring(bm, cx, cy, z1, r_in, circle)
    n = len(circle)
    for k in range(n):
        k1 = (k + 1) % n
        bm.faces.new((ob[k], ob[k1], ot[k1], ot[k]))   # outer wall
        bm.faces.new((ib[k], it[k], it[k1], ib[k1]))   # inner wall
        bm.faces.new((ot[k], ot[k1], it[k1], it[k]))   # top annulus
        bm.faces.new((ob[k], ib[k], ib[k1], ob[k1]))   # bottom annulus


def add_all_pegs(bm: bmesh.types.BMesh, params: dict) -> int:
    circle = unit_circle(params["peg_segments"])
    z0 = params["panel_thickness"] - params["peg_embed"]
    z1 = params["panel_thickness"] + params["peg_height"]
    r_out, r_in = params["peg_outer_radius"], params["peg_inner_radius"]
    positions = grid_positions(params)
    for cx, cy in positions:
        if r_in > 0:
            add_hollow_peg(bm, cx, cy, z0, z1, r_out, r_in, circle)
        else:
            add_solid_peg(bm, cx, cy, z0, z1, r_out, circle)
    return len(positions)


def boolean_union(target: bpy.types.Object, tool: bpy.types.Object) -> None:
    mod = target.modifiers.new("PegUnion", 'BOOLEAN')
    mod.operation = 'UNION'
    mod.solver = 'EXACT'
    mod.object = tool
    bpy.context.view_layer.objects.active = target
    target.select_set(True)
    bpy.ops.object.modifier_apply(modifier=mod.name)
    tool_mesh = tool.data
    bpy.data.objects.remove(tool, do_unlink=True)
    bpy.data.meshes.remove(tool_mesh)


def generate(params: dict) -> bpy.types.Object:
    check_params(params)
    panel, panel_bm = create_mesh_object("PegPanel")
    add_panel(panel_bm, params)

    if params["boolean_union"]:
        pegs, pegs_bm = create_mesh_object("Pegs")
        count = add_all_pegs(pegs_bm, params)
        finalize_bmesh(pegs, pegs_bm)
        finalize_bmesh(panel, panel_bm)
        boolean_union(panel, pegs)
    else:
        count = add_all_pegs(panel_bm, params)
        finalize_bmesh(panel, panel_bm)

    print(f"Pegs: {count}")
    return panel


def validate_printability(obj: bpy.types.Object) -> list[str]:
    issues = []
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    non_manifold = [e for e in bm.edges if not e.is_manifold]
    if non_manifold:
        issues.append(f"Non-manifold edges: {len(non_manifold)}")
    loose = [v for v in bm.verts if not v.link_edges]
    if loose:
        issues.append(f"Loose vertices: {len(loose)}")
    zero_faces = [f for f in bm.faces if f.calc_area() < 1e-6]
    if zero_faces:
        issues.append(f"Zero-area faces: {len(zero_faces)}")
    bm.free()
    return issues


if __name__ == "__main__":
    clean_scene()
    setup_print_units()
    obj = generate(PARAMS)
    issues = validate_printability(obj)
    if issues:
        print("Printability issues:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("Mesh is print-ready")
```

### Что изменилось и почему

- **Все штырьки собираются в одном `bmesh`.** Кольца вершин и грани создаются вручную, по одному кольцу на каждый уровень. Таблица cos/sin считается один раз.
- **Грани ориентированы наружу уже при построении.** `recalc_face_normals` оставлен как страховка.
- **Одна операция EXACT Union.** На ~27 тыс. граней она, скорее всего, займёт единицы секунд, но на вашей машине я это не проверял. Если всё равно тормозит, поставьте `"boolean_union": False`. Тогда получится одна сетка с перекрывающимися оболочками. `validate_printability` её пропустит, потому что каждая оболочка замкнута, но это не единое тело. Слайсеры такое обычно объединяют сами.
- **Высота считается от верхней грани панели,** поэтому штырёк торчит ровно на `peg_height`.

## Печать на Ender 3

- **Ориентация:** панелью на стол, штырьки вверх. Поддержки не нужны.
- **Панель:** 3 мм при слое 0.2 мм — это, например, 4 нижних и 4 верхних слоя. Заполнение почти не понадобится.
- **Штырьки:** на каждом слое их 432 штуки, так что будет много ретрактов и перемещений. Стоит включить в слайсере «avoid crossing perimeters» / «combing», проверить ретракт (на Ender 3 со штатным Bowden часто ставят около 5 мм — это по памяти, подберите тестом). Иначе будет паутина между штырьками.
- **Прочность:** штырёк, напечатанный вертикально, ломается по слою у основания. Если тяжёлое будет вешаться на отдельные штырьки, лучше их не делать полыми или увеличить диаметр до 4 мм.
- **Первая печать:** сначала напечатайте кусок 40×40 мм с парой штырьков. Так вы проверите и стенку 0.4/0.8, и посадку ваших крючков, не тратя ~2 часа и ~100 г пластика на всю панель (оценка грубая).


## Answer B
## Почему медленно

Три причины:

1. **`bpy.ops` в цикле.** Каждый вызов `primitive_cylinder_add` создаёт новый объект, пишет шаг undo и обновляет view layer / depsgraph. Эти расходы растут с числом объектов в сцене, поэтому 450 вызовов подряд дают почти квадратичную сложность. Отсюда минуты и подвисания.
2. **`join()` 450 объектов** тоже заметно нагружает Blender, хотя и меньше, чем цикл.
3. **Результат ещё и неправильный.** Цилиндр `depth=12` с центром на `z=7.5` занимает `z = 1.5…13.5`, то есть на 1.5 мм входит в панель. `join` объединяет меши, но не тела. Получаются 451 пересекающаяся оболочка, а не один manifold-объём. Слайсеры обычно такое переваривают, но 3D Print Toolbox покажет intersecting faces, а полость внутри штырька при таком подходе без boolean сделать не получится.

Решение: собирать всю геометрию сразу одним `bmesh`, без операторов и без boolean. Панель делится на ячейки 8×8 мм, у каждой ячейки в центре есть круглое отверстие, и стенка штырька растёт прямо из края этого отверстия. Меш получается сразу водонепроницаемым, 432–450 штырьков строятся за доли секунды (оценка, у себя в Blender я этот скрипт не запускал).

## Сначала о стенке 0.4 мм: это плохая идея

Внешний радиус 1.5 и внутренний 1.1 дают **стенку 0.4 мм, то есть ровно одну линию сопла 0.4**. Что из этого выйдет:

- Слайсер напечатает стенку одним проходом или вообще её потеряет. В Cura тонкие стенки без «Print Thin Walls» могут пропасть, в PrusaSlicer с Arachne получится одна линия.
- Трубка из одного периметра высотой 12 мм будет ломаться по слоям от первого же повешенного инструмента. Нагрузка на штырёк перфопанели изгибающая, у основания, а слои в PLA как раз перпендикулярны ей.
- Экономия есть, но небольшая. Сплошной штырёк: π·1.5²·12 ≈ 84.8 мм³. Полый: π·(1.5²−1.1²)·12 ≈ 39.2 мм³. На 432 штырька это ≈ 19.7 см³, примерно 24 г PLA. Сама панель при этом 200×150×3 = 90 см³.

**Рекомендую** `peg_inner_radius = 0.7`: стенка 0.8 мм, два периметра. Экономия около 10 г, а прочность нормальная. Ещё лучше вообще не делать полость: 2–3 периметра на диаметре 3 мм почти целиком заполняют штырёк, и экономить там почти нечего. Пластик лучше экономить на заполнении панели. В скрипте я оставил ваше значение 1.1, но при стенке тоньше двух линий сопла он выводит предупреждение. `0` делает штырёк сплошным.

## Скрипт

```python
import math
import time

import bpy
import bmesh

PARAMS = {
    "panel_width": 200.0,       # X, mm
    "panel_height": 150.0,      # Y, mm
    "panel_thickness": 3.0,     # Z, mm
    "pitch": 8.0,               # grid step, mm
    "min_margin": 2.0,          # min border between panel edge and cell grid, mm (> 0)
    "peg_radius": 1.5,          # outer radius, mm
    "peg_inner_radius": 1.1,    # 0 = solid peg; 0.7 recommended (0.8 mm wall)
    "peg_height": 12.0,         # above panel top surface, mm
    "segments": 16,             # circle resolution, must be a multiple of 8
    "nozzle": 0.4,              # mm, used for wall-thickness warning
    "material_density": 1.24,   # g/cm^3, PLA
    "object_name": "PegPanel",
}

VertKey = tuple[float, float, float]


def clean_scene() -> None:
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def setup_print_units() -> None:
    units = bpy.context.scene.unit_settings
    units.system = 'METRIC'
    units.scale_length = 0.001      # 1 Blender unit = 1 mm
    units.length_unit = 'MILLIMETERS'


def validate_params(p: dict) -> None:
    n = p["segments"]
    if n < 8 or n % 8 != 0:
        raise ValueError(f"segments={n}: must be a multiple of 8 (cell corners sit at 45 deg)")
    if p["min_margin"] <= 0:
        raise ValueError("min_margin must be > 0 (border frame is built from it)")
    if p["peg_radius"] >= p["pitch"] / 2:
        raise ValueError(f"peg_radius={p['peg_radius']} must be < pitch/2={p['pitch'] / 2}")
    inner = p["peg_inner_radius"]
    if inner < 0 or inner >= p["peg_radius"]:
        raise ValueError(f"peg_inner_radius={inner} must be in [0, peg_radius)")
    wall = p["peg_radius"] - inner
    if inner > 0 and wall < 2 * p["nozzle"]:
        print(f"WARNING: peg wall {wall:.2f} mm < 2 x nozzle ({2 * p['nozzle']:.2f} mm): "
              f"single perimeter, pegs will be fragile or dropped by the slicer")


def grid_layout(p: dict) -> tuple[int, int, float, float]:
    """Return cols, rows and lower-left corner of the centred cell grid."""
    pitch = p["pitch"]
    cols = int((p["panel_width"] - 2 * p["min_margin"]) // pitch)
    rows = int((p["panel_height"] - 2 * p["min_margin"]) // pitch)
    if cols < 1 or rows < 1:
        raise ValueError("panel too small for a single cell")
    x0 = (p["panel_width"] - cols * pitch) / 2
    y0 = (p["panel_height"] - rows * pitch) / 2
    return cols, rows, x0, y0


def ring_dirs(n: int) -> list[tuple[float, float]]:
    return [(math.cos(2 * math.pi * k / n), math.sin(2 * math.pi * k / n)) for k in range(n)]


def shared_vert(bm: bmesh.types.BMesh, cache: dict[VertKey, bmesh.types.BMVert],
                x: float, y: float, z: float) -> bmesh.types.BMVert:
    """Vertices on cell borders are shared between neighbours -> watertight surface."""
    key = (round(x, 5), round(y, 5), round(z, 5))
    vert = cache.get(key)
    if vert is None:
        vert = bm.verts.new(key)
        cache[key] = vert
    return vert


def ring(bm: bmesh.types.BMesh, dirs: list[tuple[float, float]],
         cx: float, cy: float, radius: float, z: float) -> list[bmesh.types.BMVert]:
    return [bm.verts.new((cx + radius * c, cy + radius * s, z)) for c, s in dirs]


def bridge(bm: bmesh.types.BMesh, a: list, b: list) -> None:
    n = len(a)
    for k in range(n):
        k2 = (k + 1) % n
        bm.faces.new((a[k], a[k2], b[k2], b[k]))


def add_cell(bm, cache, dirs, cx: float, cy: float, p: dict) -> list:
    """Top face of one pitch x pitch cell with a round hole; returns the hole ring."""
    top = p["panel_thickness"]
    half = p["pitch"] / 2
    hole = ring(bm, dirs, cx, cy, p["peg_radius"], top)
    square = []
    for c, s in dirs:
        scale = half / max(abs(c), abs(s))      # project direction onto the cell square
        square.append(shared_vert(bm, cache, cx + c * scale, cy + s * scale, top))
    bridge(bm, hole, square)
    return hole


def add_peg(bm, dirs, base: list, cx: float, cy: float, p: dict) -> None:
    """Peg wall grows from the cell hole ring; optional blind bore down to panel top."""
    bottom_z = p["panel_thickness"]
    top_z = bottom_z + p["peg_height"]
    outer_top = ring(bm, dirs, cx, cy, p["peg_radius"], top_z)
    bridge(bm, base, outer_top)
    inner_r = p["peg_inner_radius"]
    if inner_r <= 0:
        bm.faces.new(outer_top)
        return
    inner_top = ring(bm, dirs, cx, cy, inner_r, top_z)
    inner_floor = ring(bm, dirs, cx, cy, inner_r, bottom_z)
    bridge(bm, outer_top, inner_top)       # top annulus
    bridge(bm, inner_top, inner_floor)     # bore wall
    bm.faces.new(inner_floor)              # bore floor at panel top level


def add_frame_and_body(bm, cache, p: dict, gx0: float, gy0: float, gx1: float, gy1: float) -> None:
    """Border strips around the cell grid, side walls and bottom face."""
    w, h, top = p["panel_width"], p["panel_height"], p["panel_thickness"]
    eps = 1e-4

    def edge_verts(axis: int, value: float, sort_axis: int) -> list:
        verts = [(k, v) for k, v in cache.items() if abs(k[axis] - value) < eps]
        return [v for _, v in sorted(verts, key=lambda kv: kv[0][sort_axis])]

    bottom = edge_verts(1, gy0, 0)
    upper = edge_verts(1, gy1, 0)
    left = edge_verts(0, gx0, 1)
    right = edge_verts(0, gx1, 1)

    t_bl, t_br = bm.verts.new((0, 0, top)), bm.verts.new((w, 0, top))
    t_tr, t_tl = bm.verts.new((w, h, top)), bm.verts.new((0, h, top))
    b_bl, b_br = bm.verts.new((0, 0, 0)), bm.verts.new((w, 0, 0))
    b_tr, b_tl = bm.verts.new((w, h, 0)), bm.verts.new((0, h, 0))

    bm.faces.new([t_bl, t_br] + bottom[::-1])
    bm.faces.new([t_br, t_tr] + right[::-1])
    bm.faces.new([t_tr, t_tl] + upper)
    bm.faces.new([t_tl, t_bl] + left)

    bm.faces.new((b_bl, b_br, t_br, t_bl))
    bm.faces.new((b_br, b_tr, t_tr, t_br))
    bm.faces.new((b_tr, b_tl, t_tl, t_tr))
    bm.faces.new((b_tl, b_bl, t_bl, t_tl))
    bm.faces.new((b_bl, b_tl, b_tr, b_br))


def validate_mesh(bm: bmesh.types.BMesh) -> None:
    bad_edges = [e for e in bm.edges if not e.is_manifold]
    loose = [v for v in bm.verts if not v.link_faces]
    if bad_edges or loose:
        raise RuntimeError(f"mesh not printable: {len(bad_edges)} non-manifold edges, "
                           f"{len(loose)} loose verts")
    print(f"OK: manifold, {len(bm.verts)} verts, {len(bm.faces)} faces")


def build_peg_panel(p: dict) -> bpy.types.Object:
    validate_params(p)
    cols, rows, gx0, gy0 = grid_layout(p)
    pitch = p["pitch"]
    dirs = ring_dirs(p["segments"])
    bm = bmesh.new()
    cache: dict[VertKey, bmesh.types.BMVert] = {}

    for i in range(cols):
        for j in range(rows):
            cx = gx0 + (i + 0.5) * pitch
            cy = gy0 + (j + 0.5) * pitch
            hole = add_cell(bm, cache, dirs, cx, cy, p)
            add_peg(bm, dirs, hole, cx, cy, p)

    add_frame_and_body(bm, cache, p, gx0, gy0, gx0 + cols * pitch, gy0 + rows * pitch)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    validate_mesh(bm)

    volume_cm3 = bm.calc_volume(signed=False) / 1000.0
    print(f"{cols} x {rows} = {cols * rows} pegs, volume {volume_cm3:.1f} cm3 "
          f"(~{volume_cm3 * p['material_density']:.0f} g at 100% infill)")

    mesh = bpy.data.meshes.new(p["object_name"])
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(p["object_name"], mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


if __name__ == "__main__":
    start = time.perf_counter()
    clean_scene()
    setup_print_units()
    build_peg_panel(PARAMS)
    print(f"built in {time.perf_counter() - start:.2f} s")
```

Как это устроено:

- Геометрия получается за один проход: ~432 × (16 граней ячейки + 49 граней штырька) ≈ 28 тыс. граней. Ни одного `bpy.ops` в цикле, ни `join`, ни boolean.
- Соседние ячейки делят вершины на общих рёбрах через `shared_vert`. Поэтому верхняя поверхность панели непрерывна, а `validate_mesh` упадёт с понятной ошибкой, если где-то останется незамкнутое ребро.
- Полость глухая: её дно на уровне верхней плоскости панели, снизу панель сплошная.
- При 200×150 и `min_margin=2` получается сетка **24×18 = 432** штырька: поля 4 мм по X и 3 мм по Y, от центра крайнего штырька до края 8 и 7 мм. Если нужно 25 колонок, увеличьте `panel_width` до ~204 мм или уменьшите шаг.
- Высота 12 мм считается от верхней плоскости панели. У вас из-за `z=7.5` над панелью было только 10.5 мм.
- Все API-вызовы (`bmesh.ops.recalc_face_normals`, `BMesh.calc_volume`, `BMEdge.is_manifold` и т.д.) я пишу по памяти и в 4.1 не проверял. Если что-то не сработает, сверьтесь с docs.blender.org/api/4.1 и пришлите traceback.
- Краевые полосы рамки собраны как n-gon с коллинеарными вершинами. Это корректный manifold, но если слайсер или экспорт начнёт ругаться на вырожденные треугольники, добавьте перед `validate_mesh` строку `bmesh.ops.triangulate(bm, faces=bm.faces[:])`.

Экспорт: File → Export → STL. При `scale_length = 0.001` координаты уже в мм, но в диалоге экспорта проверьте, что Scale = 1.

## Печать на Ender 3

- **Ориентация:** панель плашмя, штырьки вверх. Поддержки не нужны: нет ни нависаний, ни мостов.
- **Панель:** 3 мм — это, например, 0.2 × 15 слоёв. Хватит 3–4 top/bottom слоёв, заполнения 15–20 % и 2–3 периметров.
- **Штырьки:** 432 тонкие башенки на каждом слое означают много перемещений и ретрактов. Включите combing / «avoid crossing perimeters» и Z-hop около 0.2–0.4 мм, чтобы сопло не сбивало штырьки. Настройте ретракт, иначе всё будет в паутине. Скорее всего, время печати будет определяться в основном перемещениями, а не экструзией.
- **Прочность:** слабое место — основание штырька (изгиб поперёк слоёв). Если штырьки будут ломаться, первым делом увеличьте стенку или диаметр. Следующий шаг — скругление у основания (здесь его нет, но могу добавить).
- **Проверка перед печатью:** в превью слайсера убедитесь, что у штырьков действительно есть стенка. При 0.4 мм она может частично исчезнуть.

