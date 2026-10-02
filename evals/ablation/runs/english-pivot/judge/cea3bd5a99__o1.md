# Case ru-jvm-oomkilled

## Conversation so far
(none)

## Latest user message
Под с сервисом на Java 21 в Kubernetes каждые пару часов перезапускается, в kubectl describe pod вижу Last State: Terminated, Reason: OOMKilled, Exit Code: 137. В контейнере limits.memory: 512Mi, приложение запускается с -Xmx512m. Почему так и что поменять?

## Rubric (what a strong answer does)
1. Explains that -Xmx caps only the heap while the JVM also uses non-heap memory (metaspace, thread stacks, code cache, direct buffers), so a 512m heap lets the process exceed the 512Mi limit and the container is OOM-killed (exit code 137).
2. Recommends a concrete fix: a heap well below the limit (for example replacing -Xmx with -XX:MaxRAMPercentage, or a smaller -Xmx) and/or a higher limits.memory, and suggests measuring actual memory use.
3. Does not mistake the symptom for a Java OutOfMemoryError: it is a container OOM kill.
4. The reply is in Russian, with code, commands and identifiers left as written, and contains no English restatement of the request.

## Answer A
**Коротко:** `-Xmx512m` ограничивает только heap, а лимит контейнера 512Mi распространяется на весь процесс JVM. Heap + всё, что вне heap, больше 512Mi. Когда heap со временем разрастается до максимума, суммарное потребление памяти выходит за лимит cgroup, и ядро убивает процесс по SIGKILL (137 = 128 + 9). Исправить это можно двумя способами: уменьшить heap, оставив запас под остальную память, или увеличить лимит.

## Почему так происходит

Процесс JVM занимает заметно больше, чем `-Xmx`:

| Область | Обычно (приблизительно, зависит от приложения) |
|---|---|
| Heap | до `-Xmx` (у вас 512m) |
| Metaspace (классы) | 50–150 MB у Spring-подобных сервисов |
| Code cache (JIT) | до ~240 MB по умолчанию, на практике обычно 30–80 MB |
| Стеки потоков | ~1 MB × число потоков (200 потоков Tomcat ≈ 200 MB зарезервировано, реально занято меньше) |
| Служебные структуры GC | несколько % от heap |
| Direct/NIO-буферы (Netty, драйверы БД) | без явного предела по умолчанию ≈ размеру heap |
| Native-библиотеки, malloc-арены glibc | десятки MB |

Цифры выше я привожу по памяти, а не проверенные измерения. Фактическую картину по вашему поду покажет NMT (см. ниже).

Почему перезапуск происходит через пару часов, а не сразу. JVM не забирает весь heap при старте. Heap растёт по мере нагрузки, и только когда он приближается к 512m, сумма heap + non-heap превышает 512Mi. Java-ошибку `OutOfMemoryError` вы при этом не увидите: JVM считает, что ей «можно» ещё, а убивает её ядро, а не она сама.

## Что поменять

**Вариант 1 (рекомендую): heap в процентах от лимита контейнера.** Java 21 читает лимит cgroup, поэтому `-Xmx` можно убрать:

```yaml
# фрагмент Deployment
spec:
  template:
    spec:
      containers:
        - name: app
          image: registry.example.com/app:1.4.2   # пинить тег/дайджест, не latest
          env:
            - name: JAVA_TOOL_OPTIONS
              # Heap = 75% лимита контейнера; остальные ~25% остаются под metaspace, стеки, code cache, direct buffers.
              # Для heavy-native приложений (Netty, много потоков) лучше 60-65%.
              value: >-
                -XX:MaxRAMPercentage=75.0
                -XX:MaxMetaspaceSize=128m
                -XX:MaxDirectMemorySize=64m
                -XX:+ExitOnOutOfMemoryError
          resources:
            requests:
              memory: 768Mi   # request = limit: под не окажется на перегруженной ноде
            limits:
              memory: 768Mi
```

- Лимит 768Mi при 75% дает heap около 576m, примерно столько же, сколько у вас сейчас, плюс ~190 MB на остальное. Если хотите остаться на 512Mi, heap получится около 384m. Хватит ли этого, зависит от того, сколько живых данных реально держит приложение. Это надо смотреть по метрикам GC, а не предполагать.
- `MaxMetaspaceSize` и `MaxDirectMemorySize` ограничивают рост этих областей. Если их превысить, получите понятную Java-ошибку вместо безымянного SIGKILL. Значения выше взяты как пример, подбирайте их по данным NMT.
- `-XX:+ExitOnOutOfMemoryError` заставляет процесс завершиться при настоящем OOM в heap, а не продолжать работу в полумертвом состоянии.

**Вариант 2 (минимальный): оставить лимит 512Mi и задать `-Xmx350m`** (примерно 65–70% лимита). Это быстрее, но heap придётся вручную держать в соответствии с лимитом при каждом изменении.

## Как проверить, что дело именно в этом

```bash
# 1. Включить Native Memory Tracking (небольшой оверхед, на время диагностики)
#    добавить в JAVA_TOOL_OPTIONS: -XX:NativeMemoryTracking=summary
kubectl exec <pod> -- jcmd 1 VM.native_memory summary   # PID 1, если java — entrypoint

# 2. Сравнить RSS процесса с лимитом
kubectl top pod <pod> --containers
# В Prometheus: container_memory_working_set_bytes{pod="<pod>"} против лимита.
# OOM killer срабатывает по нему, а не по heap.

# 3. Посмотреть, сколько реально занимает heap после GC
kubectl exec <pod> -- jcmd 1 GC.heap_info
```

Если после Full GC живые данные в heap стабильно растут и не освобождаются, значит, у вас есть ещё и утечка в приложении. Тогда увеличение лимита только отодвинет падение. В этом случае снимите heap dump (`jcmd 1 GC.heap_dump /tmp/heap.hprof`) и разберите его.

## Риски и откат

- **Что затронет:** только этот Deployment. Изменение вызовет rolling update всех реплик. Если поднимаете request, проверьте, что на нодах хватает allocatable памяти, иначе новые поды останутся в `Pending`.
- **Если heap окажется мал**, это будет видно по частым GC и `OutOfMemoryError: Java heap space` в логах. Тогда увеличьте лимит, оставляя процент прежним.
- **Откат:** `kubectl rollout undo deployment/<name>` или revert коммита в манифестах/Helm values, если деплой идёт через GitOps.
- **Стоимость:** +256Mi на каждую реплику. На маленьком кластере это может потребовать дополнительную ноду.


## Answer B
**Коротко:** `-Xmx512m` ограничивает только heap, а лимит 512Mi распространяется на весь процесс JVM. Heap плюс off-heap память гарантированно превышают 512Mi, поэтому ядро (cgroup OOM killer) убивает контейнер. Java при этом не успевает бросить `OutOfMemoryError`, она просто получает SIGKILL. Exit Code 137 = 128 + 9 (SIGKILL).

## Почему так происходит

Память JVM состоит не только из heap:

- **Heap**: до `-Xmx`, у вас 512m, то есть уже весь лимит.
- **Metaspace**: метаданные классов. По умолчанию он не ограничен; у Spring-приложения обычно занимает 80–150 МБ.
- **Code cache**: JIT-код, обычно 30–70 МБ, максимум около 240 МБ (`ReservedCodeCacheSize`; значение по памяти, не проверено).
- **Стеки потоков**: около 1 МБ на поток (`-Xss`). При 200 потоках Tomcat это до 200 МБ виртуальной памяти, реально в RSS попадает меньше.
- **Direct/NIO-буферы**: Netty, драйверы БД, HTTP-клиенты. По умолчанию `MaxDirectMemorySize` ≈ `-Xmx` (по памяти, не проверено).
- **Структуры GC, malloc-арены glibc, JNI**.

Почему контейнер падает раз в пару часов, а не сразу: JVM занимает heap постепенно. Сначала RSS небольшой, потом под нагрузкой heap дорастает до своего потолка. Как только heap + non-heap переходят 512Mi, приходит OOMKill. Обычно реальный RSS при `-Xmx512m` составляет 650–800 МБ. Это оценка: точная цифра зависит от приложения.

## Что поменять

**Вариант 1. Задавать heap как долю от лимита контейнера (рекомендую).** Начиная с JDK 10 JVM видит cgroup-лимиты, а Java 21 поддерживает cgroup v2. Уберите `-Xmx` и задайте процент:

```yaml
resources:
  requests:
    memory: "768Mi"
    cpu: "500m"
  limits:
    memory: "768Mi"   # requests == limits: под не вытеснят первым при давлении на ноду
env:
  - name: JAVA_TOOL_OPTIONS
    # Heap ~75% от лимита (~576Mi); остальное уходит на metaspace, code cache, стеки, direct buffers.
    # MaxMetaspaceSize ограничивает metaspace, чтобы утечка classloader'ов давала понятную ошибку, а не OOMKill.
    # ExitOnOutOfMemoryError: при heap OOM процесс завершится сразу, а не будет висеть "полуживым".
    value: >-
      -XX:MaxRAMPercentage=75.0
      -XX:MaxMetaspaceSize=192m
      -XX:+ExitOnOutOfMemoryError
```

Если heap больше 512m на самом деле не нужен, оставьте лимит 512Mi и поставьте `MaxRAMPercentage=65`, тогда heap будет около 330 МБ. Сначала проверьте, что приложению такого heap хватает: смотрите GC-логи или метрику `jvm_memory_used_bytes{area="heap"}` после GC.

**Вариант 2. Явные значения.** Подойдёт, если нужен полный контроль: `-Xmx384m -XX:MaxMetaspaceSize=128m -XX:ReservedCodeCacheSize=64m -XX:MaxDirectMemorySize=64m -Xss512k` при лимите 640–768Mi. Минус в том, что при изменении лимита эти флаги придётся пересчитывать вручную.

Эмпирическое правило: heap ≤ 70–75% от `limits.memory`, а на non-heap оставлять не меньше 150–250 МБ.

## Как проверить, куда уходит память

Включите Native Memory Tracking. Он добавляет немного накладных расходов, поэтому лучше включать его временно или на одном поде:

```bash
# Добавить в JAVA_TOOL_OPTIONS: -XX:NativeMemoryTracking=summary
kubectl exec -it <pod> -- jcmd 1 VM.native_memory summary

# Реальное потребление контейнера и рестарты
kubectl top pod <pod>
kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[0].restartCount}'

# Какие флаги JVM фактически применила
kubectl exec -it <pod> -- java -XX:+PrintFlagsFinal -version | grep -E 'MaxHeapSize|MaxRAMPercentage'
```

Здесь `1` — это PID Java-процесса в контейнере, если java запущена как entrypoint. Если в образе нет `jcmd` (JRE-образ без JDK-утилит), используйте образ с JDK или `kubectl debug`.

Если NMT показывает, что heap и известные области стабильны, а RSS продолжает расти, то это, скорее всего, нативная утечка или фрагментация glibc malloc. Помогает `MALLOC_ARENA_MAX=2` в env. Если растёт `Internal`/`Other`, ищите direct buffers.

После изменения в течение суток смотрите на `container_memory_working_set_bytes`: эту метрику учитывает kubelet. Рестарты должны прекратиться, а пик RSS должен оставаться ниже лимита с запасом 10–15%.

## Риски и откат

- **Blast radius:** только этот Deployment. Изменение `limits` и `requests` пересоздаст поды через rolling update. Если на нодах не хватит свободной памяти под новый request 768Mi, поды останутся в `Pending`. Проверьте `kubectl describe nodes` (Allocatable) и ResourceQuota namespace.
- **Стоимость:** +256Mi на реплику резервируется на ноде, и при большом числе реплик это может потребовать дополнительных нод.
- **Откат:** `kubectl rollout undo deployment/<name>`. Если манифесты в Git/ArgoCD, откатите коммит, чтобы не было drift.

