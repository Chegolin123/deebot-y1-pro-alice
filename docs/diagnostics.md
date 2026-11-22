# Диагностика: команды

Набор проверок, которым найдена причина `(406,10025)`. Порядок важен: от
устройства к облаку.

## 1. Робот в сети?

На роутере: **DHCP Client List**. Робот представляется именем `Livirobo`.

Если у вас Linux-шлюз:

```bash
ip -4 neigh | grep -i livirobo
```

Помните про рандомизацию MAC: приложение может показывать адрес с другим
первым байтом (`AA:` вместо `A8:`).

## 2. Что робот спрашивает у DNS

`dnsmasq` пишет источник каждого запроса:

```bash
journalctl -u dnsmasq --since "-10 min" | grep <IP_РОБОТА>
```

Ищите повторяющиеся `query` с ответом `NXDOMAIN` — это и есть зацикленный
эндпоинт.

Если DNS не `dnsmasq`, подойдёт любой сниффер:

```bash
tcpdump -ni any port 53 and host <IP_РОБОТА>
```

## 3. Имя живое?

```bash
dig +short @77.88.8.8 jmq2-ngiot-ru.dc.robotww.ecouser.net A
dig +short @8.8.8.8  jmq2-ngiot-ru.dc.robotww.ecouser.net A
dig +short @1.1.1.1  jmq2-ngiot-ru.dc.robotww.ecouser.net A
```

`NXDOMAIN` у всех трёх — имени нет в зоне, дело не в вашем резолвере.

Сравните с рабочим региональным вариантом:

```bash
dig +short @1.1.1.1 jmq2-ngiot-eu.dc.robotww.ecouser.net A
```

## 4. Сертификат на SLB

Проверка, покроет ли TLS имя при DNS-подмене:

```bash
echo | openssl s_client -connect 37.230.195.108:8883 \
  -servername jmq2-ngiot-ru.dc.robotww.ecouser.net 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName
```

Ожидание: `CN=*.ecouser.net` и SAN с `*.dc.robotww.ecouser.net`.

## 5. Проба через библиотеку

`probe/` показывает, что видит аккаунт и приходит ли телеметрия:

```bash
docker build -t deebot-probe ./probe
docker run --rm -it deebot-probe
```

Скрипт только читает: список устройств, заряд, состояние. Команд не отправляет.

## Типичные выводы

| Наблюдение | Вывод |
| --- | --- |
| Робота нет в DHCP | проблема Wi-Fi/ассоциации, не облака |
| Робот есть, DNS-запросы идут, ответ `NXDOMAIN` | мёртвый эндпоинт — менять регион аккаунта |
| Робот есть, DNS молчит | робот не дошёл до резолва: проверьте шлюз/DNS в DHCP |
| `probe` не видит устройство | аккаунт/регион не совпадают с тем, к которому привязан робот |
| `probe` видит, состояние пустое | профиль устройства не поддержан библиотекой |
