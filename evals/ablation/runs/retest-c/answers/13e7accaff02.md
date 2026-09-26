```yaml
    - alert: PaymentsApiPodCrashLooping
      expr: increase(kube_pod_container_status_restarts_total{namespace="payments", pod=~"payments-api-.*"}[15m]) > 3
      for: 1m
      labels:
        severity: warning
        team: payments
      annotations:
        summary: "payments-api: под {{ $labels.pod }} перезапустился >3 раз за 15 минут"
        impact: "Меньше живых реплик, часть оплат падает или тормозит"
        mitigation: "kubectl rollout undo deployment/payments-api -n payments"
        dashboard: "https://grafana.internal/d/payments-api"
    - alert: PaymentsDbConnectionsExhausted
      expr: |
        sum by (namespace, pod) (pgbouncer_pools_client_active_connections{namespace="payments-db"})
          / on (namespace, pod)
        max by (namespace, pod) (pgbouncer_config_max_client_connections{namespace="payments-db"})
          > 0.9
      for: 5m
      labels:
        severity: critical
        team: payments
      annotations:
        summary: "pgbouncer в payments-db: занято >90% клиентских соединений"
        impact: "Новые подключения к БД отклоняются, оплаты падают"
        mitigation: "kubectl rollout restart deployment/payments-api -n payments"
        dashboard: "https://grafana.internal/d/<pgbouncer-dashboard>"
```

Что проверить перед коммитом:

- **Dashboard для pgbouncer** оставил плейсхолдером, реальный URL я не знаю. Если CI его не валидирует, он так и уедет на страницу, поэтому подставьте ссылку. Для CrashLooping поставил дашборд payments-api, как у первого алерта.
- **Метрики и лейблы.** `kube_pod_container_status_restarts_total` берётся из kube-state-metrics, `pgbouncer_*` из pgbouncer-exporter. Имена я помню, но здесь не проверял. Убедитесь, что у вас есть лейблы `namespace` и `pod`: у экспортера за ServiceMonitor они обычно есть, при static scrape их может не быть. Тогда матчите `on (instance)`.
- **Почему в выражении `sum` / `max`.** `client_active_connections` отдаётся отдельно по каждому пулу (database/user), а `max_client_connections` задаётся одной настройкой на весь инстанс. Без агрегации деление не сматчится. Ещё учтите, что в лимит `max_client_conn` входят и ожидающие клиенты. Если хотите считать честно, прибавьте в числитель `pgbouncer_pools_client_waiting_connections`.
- **Mitigation для CrashLooping.** `rollout restart` при crashloop не поможет, поэтому взял `rollout undo`, как у первого алерта. Это верный шаг, если падения начались после деплоя. Если причина в зависимости (БД, секрет, OOM), откат ничего не даст, и тогда смотрите `kubectl logs --previous`.
- **Mitigation для DbConnectionsExhausted.** Здесь я исхожу из того, что основной клиент pgbouncer это payments-api и соединения течёт именно он. Рестарт приложения освободит клиентские коннекты. Рестартить сам pgbouncer хуже: он оборвёт вообще всех. Если клиентов несколько, укажите того, кто держит больше всего.
- **Severity** у CrashLooping поставил `warning`: один падающий под при нескольких репликах ещё не outage. Если реплика одна, поднимите до `critical`.
