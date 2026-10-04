# TODO

1. Validar la guia de deployment amb els agents i les persones, i provar-la al
   grup "prebenjamins". Encara no hi ha evidència que això s'hagi fet.
2. Acabar l'acceptació dels cicles de correcció i review: la revisió local,
   la correcció global, l'administrador i les aprovacions locals i globals es
   validen cada dia amb `make test-e2e-telegram` (passos 07 a 10), però **l'escala
   a l'admin quan el DM privat no arriba** encara no té cap pas de proves que el
   faci passar de debò.
3. Fer arribar el resum diari sense el cron de Cloudflare: la ruta funciona i
   es pot lliçar (`make test-e2e-telegram`, pas 12), però res la crida. Al pla
   Free un cron rep 10 ms de CPU i l'intèrpret Python en necessita 3,4 s, de
   manera que `Default.scheduled` no arriba ni a la primera línia i
   `daily_report_state` es queda buida. Cal apuntar un planificador extern
   (GitHub Actions) a `POST /internal/jobs/daily-report` o subscriure al pla de
   pagament. Detall a [operations.md](docs/operations.md).
4. Decidir si el SDK de Logfire ha d'anar dins el bundle de producció. Avui
   `KB_LOGFIRE_ENABLED=false` i no s'ha de tocar: amb el tracing encès el Worker
   mor amb *Worker exceeded resource limits* perquè l'SDK d'OpenTelemetry no
   cabeix als 128 MB de l'isolat. La pregunta oberta no és el Logfire sinó si
   la seva dependència ha de continuar viatgent al bundle quan el bot no el pot
   fer servir.
