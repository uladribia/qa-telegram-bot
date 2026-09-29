1. Validar la guia de deployment amb els agents i les persones, i provar-la al grup "prebenjamins".
2. Completar l'acceptació manual dels cicles de correcció i review amb dos grups reals: reviewer local, reviewer global, admin, approvals locals/globals i escalat quan el DM privat no arriba.
3. Millorar la detecció de preguntes en els missatges de fons.
4. Permetre que l'usuari escrigui al bot per DM, detecti a quins grups pertany i resolgui el context quan hi hagi més d'un espai.
5. Implementar els modes off, silent, active i proactive, tant per DM com per grup.
6. Implementar Telethon per a proves E2E sense humans.
7. Fer arribar el resum diari sense el cron de Cloudflare: al pla Free un cron rep 10 ms de CPU i l'intèrpret Python en necessita 3,4 s, de manera que `Default.scheduled` no arriba ni a la primera línia i `daily_report_state` es queda buida. Cal apuntar un planificador extern (GitHub Actions) a `POST /internal/jobs/daily-report` o subscriure al pla Workers de pagament; la ruta ja envia i persisteix l'estat, el que falla és només qui la crida.
8. Considerar si hem de liquidar logfire (no es pot fer servir en prod ara mateix)
