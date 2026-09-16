# memegen

Gerador de memes com IA sobre a curadoria de templates do 9GAG.

Você descreve uma situação — um desabafo, um trecho de conversa, um bug, uma
reunião — e recebe os memes que melhor se aplicam, com o texto de cada caixa
preenchido e a imagem pronta.

**Um arquivo, zero dependências.** Só a biblioteca padrão, de Python 3.6 a 3.14.
Não há `pip install`, `requirements.txt` nem ambiente virtual: copie
`memegen.py` para qualquer máquina com Python e rode.

```console
$ ./memegen.py sync                 # baixa o catálogo (836 templates)
$ ./memegen.py enrich --wait        # gera os contratos de uso — ~US$ 0,84
$ ./memegen.py serve                # interface web em localhost:8000
```

## Onde cada coisa roda

A renderização acontece **no navegador**, em canvas. Não é contorno da falta de
Pillow: é o que o próprio 9GAG faz, e resolve de graça o que era trabalhoso em
Python — fonte Impact, contorno, rotação — usando o motor de texto do navegador.
De quebra, editar um texto passa a ser instantâneo, sem ida e volta ao servidor.

A consequência é que `make` na linha de comando entrega a escolha e o texto, não
a imagem. Para imagem, use `serve`.

| Parte | Onde | Com o quê |
|---|---|---|
| sincronizar, enriquecer, gerar | Python | `urllib` |
| servidor | Python | `http.server` |
| renderizar, editar, copiar, compartilhar | navegador | canvas |
| empacotar em ZIP | Python | `zipfile` |

## Como funciona

O gerador do 9GAG é totalmente estático — não existe API. O catálogo de 836
templates vive dentro de um bundle JavaScript, e cada template traz a história
do meme, as keywords e a geometria exata das caixas de texto.

O que o 9GAG **não** tem é busca semântica: a busca deles é um `includes()` de
substring sobre nome e keywords. Digitar "meu chefe pediu hora extra de novo"
retorna zero resultados. É essa camada que este projeto acrescenta.

### sync — acompanhar a curadoria do 9GAG

O nome do bundle é derivado do conteúdo, o que dá um jeito barato de detectar
mudança:

```
/meme-generator/                  (8 KB)  ->  assets/manifest-<build>.js
/assets/manifest-<build>.js       (4 KB)  ->  meme-templates-<hash>.js
/assets/meme-templates-<hash>.js (1,4 MB) ->  o catálogo
```

Os dois primeiros passos custam 12 KB e bastam para saber se algo mudou. O
bundle grande só é baixado quando o hash muda. O diff é por `source_hash` sobre
os campos que afetam o significado: mudou a miniatura, não reprocessa; mudou a
descrição ou a geometria das caixas, reprocessa só aquele template.

### enrich — de metadado para contrato de uso

O 9GAG diz o que o meme é, mas não diz **qual texto vai em qual caixa**. Em
`distracted_boyfriend` a ordem das caixas é mulher-de-vermelho, namorado,
namorada — trocar isso destrói a piada. Só 40 dos 836 descrevem os papéis
explicitamente.

O enriquecimento resolve isso uma vez por template: manda a imagem, a descrição
do 9GAG e a posição de cada caixa, e recebe um contrato em português:

```json
{
  "funcao": "Contrastar duas opções, rejeitando a primeira",
  "quando_usar": ["preferência clara", "upgrade de ferramenta"],
  "tom": ["sarcástico", "leve"],
  "slots": [
    {"box_id": "text-0", "papel": "a opção rejeitada", "max_chars": 60},
    {"box_id": "text-1", "papel": "a opção preferida", "max_chars": 60}
  ],
  "nsfw": false
}
```

Roda pela Batch API (metade do preço) e é incremental — quando o 9GAG publica 20
memes novos, você enriquece 20.

### serve — a interface

Duas abas. **Gerar** recebe a situação e devolve os memes renderizados, com um
campo por caixa, cada um rotulado com o *papel* daquela caixa e com contador
contra o limite do contrato. Editar redesenha na hora.

Cada meme tem **Compartilhar**, **Copiar**, **Baixar**, e com mais de um
resultado aparece **Baixar todos** (ZIP nomeado pela situação). O que vai para o
clipboard e para o ZIP é o desenho atual do canvas — suas edições incluídas.

**Templates** navega e busca os 836. A busca casa também pela função do
contrato, então "dilema" encontra o Two Buttons. Clicar abre a história do meme,
para que serve, quando usar e o papel de cada caixa.

Dois modos de seleção:

| Modo | Custo/meme (Opus 5) |
|---|---:|
| `shortlist` — um modelo barato peneira antes | US$ 0,060 |
| `full` — catálogo inteiro (92 mil tokens) no contexto | US$ 0,483 |

### Usando do celular

Copiar e compartilhar exigem contexto seguro. `localhost` conta;
`http://192.168.0.10:8000` não — e é assim que o celular alcança sua máquina.

```console
$ ./memegen.py serve --host 0.0.0.0 --https
memegen em https://localhost:8000
  na rede: https://192.168.0.10:8000
```

O certificado autoassinado é gerado na primeira vez pelo `openssl` da máquina
(sem dependência nova) e cobre `localhost` mais os IPs detectados. É aceitar uma
vez por aparelho.

Sobre compartilhar: **não existe URL do WhatsApp que carregue imagem** — `wa.me`
aceita só `?text=`. O botão usa a Web Share API com arquivo, que abre a folha de
compartilhamento do sistema, onde o WhatsApp aparece. Funciona em Android, iOS,
Windows e macOS; não existe em Linux desktop nem no Firefox, onde o botão fica
desabilitado explicando por quê. Aí o caminho é **Copiar** e colar.

O servidor não tem autenticação. Com `--host 0.0.0.0` fica visível para toda a
rede local: use em rede de casa, não em wi-fi público.

## Deploy — atrás de Apache e Cloudflare

O `--https` com certificado autoassinado resolve o contexto seguro na rede
local, mas pede aceite em todo aparelho. Para um hostname de verdade o caminho
é terminar o TLS antes de chegar na máquina:

```
navegador --HTTPS--> Cloudflare --HTTP--> Apache :80 --> memegen 127.0.0.1:8000
```

O serviço escuta só em `127.0.0.1`: mesmo com o firewall aberto por engano,
ninguém alcança a porta 8000 de fora. E como o navegador enxerga HTTPS,
**Copiar e Compartilhar funcionam** — é a razão de pôr algo na frente.

Validado em RHEL 8.10 com Python 3.6.8, a máquina que motiva a restrição de
versão. As 118 passam lá.

### O que precisa ir junto

`dados/` não é versionado, e **as imagens têm que ir com os contratos**. O
motivo não é óbvio: `_resumo` mede a largura real lendo o cabeçalho do JPEG
local e, quando o arquivo não existe, cai para a dimensão declarada do
catálogo — a que diverge em 779 dos 836. Como a imagem só é baixada quando o
navegador pede `/template/<id>.jpg`, ou seja *depois* da sugestão já ter sido
respondida, a primeira renderização de cada template sairia com o texto fora do
lugar e a segunda certa. Defeito que não reproduz, dos caros de achar.

O `enrich` chama `garantir_imagens`, mas só para os templates pendentes: com os
contratos completos não há pendente e nada é baixado. Até existir um comando só
para isso, copie as imagens.

```console
$ rsync -avz dados/ usuario@servidor:/opt/memegen/dados/
```

### A chave

O `EnvironmentFile` do systemd não é shell: sem `export`, sem aspas (viram
parte do valor) e sem espaço em volta do `=` (a variável fica vazia, em
silêncio).

```
# /etc/memegen.env  —  chmod 600, dono root
GEMINI_API_KEY=AIza...
MEMEGEN_PROVIDER=gemini
```

O `MEMEGEN_PROVIDER` explícito evita que uma `ANTHROPIC_API_KEY` esquecida no
ambiente troque de provedor sozinha — e com conta sem crédito isso vira um
"credit balance is too low" vindo do nada.

O systemd lê o arquivo como root antes de baixar para o usuário do serviço,
então 0600 não atrapalha. Trocar a chave exige `restart`, não `reload`: o
arquivo só é lido quando o processo nasce.

### systemd

```ini
# /etc/systemd/system/memegen.service
[Unit]
Description=memegen - gerador de memes com IA
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=opc
Group=opc
WorkingDirectory=/opt/memegen
EnvironmentFile=/etc/memegen.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/memegen/memegen.py serve --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5s

NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
ReadWritePaths=/opt/memegen/dados

[Install]
WantedBy=multi-user.target
```

`PYTHONUNBUFFERED=1` não é enfeite. Sob o systemd a saída vai para um pipe do
journal e o Python passa a bufferizar em bloco: sem isso um erro fica invisível
no `journalctl` até o buffer encher. O `--debug` também depende disso para ser
útil.

`ProtectSystem=strict` deixa o sistema de arquivos inteiro somente-leitura,
inclusive o próprio `memegen.py`; `ReadWritePaths` reabre só `dados/`, que é o
único lugar onde o programa escreve — o `.tmp` da troca atômica fica ao lado do
arquivo final, e o único `tempfile.mkstemp` está no caminho do certificado
autoassinado, coberto pelo `PrivateTmp`.

`NoNewPrivileges=yes` fecha a escalada por `sudo`, que importa quando o serviço
roda com um usuário que tem sudo sem senha.

`RestrictSUIDSGID` ficaria bem aqui, mas só existe do systemd 242 em diante e o
RHEL 8 tem o 239.

### Apache

```apache
# /etc/httpd/conf.d/memegen.conf
<VirtualHost *:80>
    ServerName memegen.exemplo.com.br

    <IfModule pagespeed_module>
        ModPagespeed off
    </IfModule>

    ProxyRequests Off
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:8000/ timeout=120
    ProxyPassReverse / http://127.0.0.1:8000/

    ErrorLog  logs/memegen_error.log
    CustomLog logs/memegen_access.log combined
</VirtualHost>
```

`ModPagespeed off` é obrigatório onde o `mod_pagespeed` estiver carregado. Ele
reescreve HTML, CSS e JS em voo — minifica, combina, adia script. A página é um
arquivo só com `<style>` e `<script>` embutidos e render em canvas: remexer
nela quebra o desenho sem deixar rastro no código-fonte.

`timeout=120` porque a geração soma triagem e escrita, cada uma com retentativa,
e o padrão de 60s cortaria no meio.

No SELinux, o Apache precisa de `httpd_can_network_connect` ligado para
alcançar a porta local:

```console
$ sudo setsebool -P httpd_can_network_connect 1
```

### Atualizar o código

Como o usuário do serviço, **nunca como root** — arquivo criado por root em
`/opt/memegen` vira arquivo que o serviço não consegue escrever depois.

```console
$ cd /opt/memegen && git pull
$ python3 -m unittest test_memegen -q > /dev/null 2>&1; echo "testes=$?"
$ sudo systemctl restart memegen
```

O `restart` é necessário porque o processo carrega o código na memória.
`dados/` não é versionado, então o `pull` nunca encosta no catálogo, nos
contratos nem nas imagens.

### sync e enrich automáticos

O 9GAG publica template novo de vez em quando; o `sync` detecta pelo hash do
bundle e o `enrich` escreve o contrato dos que chegaram. Um timer dá conta.

```ini
# /etc/systemd/system/memegen-sync.service
[Unit]
Description=memegen - sincroniza o catalogo do 9GAG e enriquece o que chegou
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=opc
Group=opc
WorkingDirectory=/opt/memegen
EnvironmentFile=/etc/memegen.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/memegen/memegen.py sync
ExecStart=/usr/bin/python3 /opt/memegen/memegen.py enrich --pausa 6

NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/memegen/dados
```

```ini
# /etc/systemd/system/memegen-sync.timer
[Unit]
Description=memegen - sync diario do catalogo do 9GAG

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
```

Dois `ExecStart` num `Type=oneshot` rodam em sequência, e o segundo só roda se
o primeiro sair com zero — se o `sync` falhar, o `enrich` nem começa.

`Persistent=true` importa em máquina que fica desligada: sem ele, um disparo
perdido é perdido para sempre; com ele, o timer roda logo depois do boot
seguinte.

Nada disso exige reiniciar o `memegen`. O cache em memória compara o mtime de
`catalogo.json` e `contratos.json` a cada leitura e recarrega sozinho.

Custo de uma rodada sem novidade: 12 KB para descobrir que o bundle não mudou,
e o `enrich` sai na hora por não ter pendente. O `enrich.lock` impede que o
timer atropele uma execução manual.

```console
$ sudo systemctl daemon-reload
$ sudo systemctl enable --now memegen-sync.timer
$ sudo systemctl start memegen-sync.service     # testa agora, sem esperar
$ journalctl -u memegen-sync -n 30 --no-pager
```

### Cloudflare

Registro `A` para o hostname apontando no IP do servidor, com o proxy **ligado**
(nuvem laranja) — é ele que entrega o HTTPS e esconde o IP de origem. Com o
Apache atendendo em HTTP puro, o modo SSL/TLS da zona é *Flexible*. Ligue
*Always Use HTTPS*: se a página abrir em HTTP, `isSecureContext` fica falso e
Copiar e Compartilhar voltam a ficar desabilitados.

Duas coisas para saber antes de estranhar:

**O Cloudflare corta requisição em 100 segundos** no plano gratuito, com erro
524, e isso não é configurável fora do Enterprise. O caminho normal passa longe
— a triagem mede 3,7s — mas um retry com backoff estoura, e aí você vê "524" no
lugar do "429, cota excedida". Se encostar nisso, a saída é `/api/gerar`
responder na hora e o resultado ser consultado depois.

**O programa não tem autenticação.** Exposto num hostname público, qualquer um
que o descubra gera meme com a sua cota. O Cloudflare Access resolve de graça,
autenticando antes de a requisição sair do Cloudflare.

## Geometria

As coordenadas das caixas não estão em pixels da imagem — estão num canvas de
640 px de largura e altura proporcional. Verificado nos 836 templates: nenhuma
caixa ultrapassa esses limites e 744 encostam exatamente em x=640.

O `width`/`height` do catálogo **não** é a dimensão da imagem servida: é a do
original. Medido, 779 dos 836 divergem — o Drake diz 1200×1200 e é servido em
640×640. Por isso a escala de render sai da largura real do arquivo, nunca da
declarada. Os memes saem com 640 px, que é o teto do que o 9GAG serve.

## Comandos

| Comando | O que faz |
|---|---|
| `sync` | verifica o 9GAG e atualiza o espelho local |
| `enrich` | enriquece os templates cujo contrato está desatualizado |
| `enrich --collect LOTE` | coleta um lote já enviado |
| `serve` | abre a interface web |
| `make "situação"` | sugere memes na linha de comando (texto, sem imagem) |
| `status` | estado local |
| `audit` | contratos que merecem revisão manual |

`audit` ordena por risco — muitas caixas, descrição sem instrução de uso. São os
candidatos a errar o papel dos slots.

## Provedores

Funciona com **Claude** ou **Gemini**. A chave presente decide sozinha; só
precisa de `MEMEGEN_PROVIDER` se as duas estiverem definidas.

```console
$ export GEMINI_API_KEY='...'      # ou ANTHROPIC_API_KEY
$ ./memegen.py status              # mostra o provedor e os modelos escolhidos
```

A diferença prática é o custo de entrada: a API da Anthropic é pré-paga, sem
tier gratuito; o Gemini tem cota mensal gratuita. Para um projeto pessoal que
enriquece uma vez e gera alguns memes por dia, a cota gratuita costuma bastar.

| | Anthropic | Gemini |
|---|---|---|
| Enriquecer os 836 | Batch API, ~1h, US$ 0,84 | sequencial, mais lento, dentro da cota |
| Endpoint | `/v1/messages` | `/v1beta/interactions` |
| Padrão enriquecer | `claude-sonnet-5` | `gemini-3.5-flash-lite` |
| Padrão gerar | `claude-opus-5` | `gemini-3.8-flash` |

Só um enriquecimento roda por vez: uma trava em `dados/enrich.lock` recusa a
segunda execução. Dois em paralelo gastam cota em duplicata e um sobrescreve os
contratos do outro. Trava de processo morto é liberada sozinha.

No Gemini o enriquecimento roda **um template por vez**, porque não há API de
lote. Ele grava depois de cada um: se a cota estourar ou você der `Ctrl + C`, o
que já foi feito fica salvo e a próxima execução continua de onde parou. Use
`--pausa` para espaçar as chamadas.

Sobre o `403` do Google: ele é ambíguo. Vem tanto para permissão de verdade
quanto para limite de taxa disfarçado — a diferença está no `reason` dentro do
corpo, não no código. Por isso a mensagem de erro mostra o corpo completo
(status, motivo e mensagem), e o cliente repete com espera crescente só quando
o motivo indica limite de taxa. Permissão de verdade falha na hora, sem
insistir à toa.

## Variáveis de ambiente

| Variável | Para quê |
|---|---|
| `ANTHROPIC_API_KEY` | chave da Anthropic |
| `ANTHROPIC_WORKSPACE_ID` | só para chave de **organização** (a API recusa sem o header dizendo qual workspace usar); chave escopada a um workspace dispensa |
| `GEMINI_API_KEY` | chave do Gemini — <https://aistudio.google.com/apikey> |
| `MEMEGEN_PROVIDER` | `anthropic` ou `gemini`, quando as duas chaves existem |
| `MEMEGEN_ENRICH_MODEL` | sobrescreve o modelo do enriquecimento |
| `MEMEGEN_TRIAGE_MODEL` | sobrescreve o modelo da triagem |
| `MEMEGEN_GENERATE_MODEL` | sobrescreve o modelo da geração |

`sync` e a navegação de templates funcionam sem chave nenhuma.

## Depuração

```console
$ ./memegen.py serve --debug        # ou enrich --debug, make --debug
$ MEMEGEN_DEBUG=1 ./memegen.py serve
```

Imprime em stderr cada chamada ao modelo: a etapa, o modelo, o tamanho da
carga, o `generation_config` enviado, a tentativa atual, o tempo decorrido e a
resposta. O base64 das imagens aparece resumido, senão enterraria o resto.

Serve para responder "por que está lento" sem adivinhar — mostra se o tempo
está na triagem ou na geração, se o retry está rodando, e o que de fato foi
enviado.

## Testes

```console
$ python3 -m unittest -v        # 118 testes, também sem dependências
```

Não precisam de rede nem de chave: o catálogo vem de um fixture embutido e as
chamadas à API são substituídas. Incluem um teste que falha se `memegen.py`
passar a importar algo fora da biblioteca padrão, e outro que falha se aparecer
sintaxe posterior ao 3.6.

## Dados

Tudo em `dados/` é derivado e reconstrói com `sync` + `enrich`. O catálogo é
espelho de conteúdo público do 9GAG.
