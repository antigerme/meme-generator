# memegen

Gerador de memes com IA construído sobre a curadoria de templates do 9GAG.

Você descreve uma situação — um desabafo, um trecho de conversa, um bug, uma
reunião — e recebe de volta os memes que melhor se aplicam, já com o texto de
cada caixa preenchido e a imagem renderizada.

```console
$ memegen make "passei a tarde inteira otimizando uma query que roda uma vez por mês"

1. Gru's Plan  (grus_plan)
   O meme serve para planos que desandam na própria conclusão lógica.
   [text-0] Otimizar a query lenta
   [text-1] Passar a tarde nisso
   [text-2] Ela roda uma vez por mês
   [text-3] Ela roda uma vez por mês
   -> out/01_grus_plan.jpg
```

## Interface web

```console
$ memegen serve
memegen em http://127.0.0.1:8000
```

Duas abas. **Gerar** recebe a situação e devolve os memes já renderizados, com
um campo por caixa de texto — cada um rotulado com o *papel* daquela caixa ("a
opção rejeitada", "quem enfrenta o dilema") e com contador de caracteres contra
o limite do contrato. Editar qualquer campo re-renderiza a imagem na hora, sem
chamar o modelo de novo: texto de meme quase sempre precisa de um retoque, e
ter que regerar tudo para trocar uma palavra mataria o uso.

**Compartilhar** abre a folha de compartilhamento do sistema com o PNG em
anexo, e é de lá que sai o WhatsApp. Não existe URL do WhatsApp que carregue
imagem — `wa.me` aceita só `?text=` —, então a Web Share API com arquivo é o
único caminho real. Ela exige contexto seguro e um sistema com folha de
compartilhamento: funciona em Android, iOS, Windows e macOS, e não existe em
Linux desktop nem no Firefox, onde o botão fica desabilitado explicando por quê.

**Copiar** põe a imagem na área de transferência, pronta para colar no WhatsApp
ou no Slack. A conversão para PNG acontece na própria página, a partir da imagem
já carregada — o clipboard do Chromium recusa JPEG. Onde o navegador não
permitir, o botão fica desabilitado e explica no título.

Com mais de um resultado aparece **Baixar todos**, que empacota num ZIP nomeado
pela situação (`memegen-escolher-entre-dormir-cedo-20260914-1804.zip`). O ZIP é
montado a partir dos campos como estão na tela, não do que o modelo escreveu —
suas edições vão junto.

**Templates** navega e busca os 836. A busca casa também pela função do
contrato, então "dilema" encontra o Two Buttons — coisa que a busca do próprio
9GAG, que é `includes()` sobre nome e keywords, não faz. Clicar num template
abre a história do meme, para que serve, quando usar e o papel de cada caixa.

### Usando do celular

Copiar e compartilhar exigem contexto seguro. `localhost` conta; `http://192.168.0.10:8000`
não — e é assim que o celular alcança sua máquina. Sem HTTPS os dois botões
ficam mortos justamente no aparelho onde o WhatsApp está.

```console
$ memegen serve --host 0.0.0.0 --https
memegen em https://localhost:8000
  na rede: https://192.168.0.10:8000
```

O certificado autoassinado é gerado na primeira vez (via `openssl`, sem
dependência nova) e cobre `localhost` mais os IPs de rede detectados — assim o
celular avisa só que o certificado é autoassinado, e não também que o nome
diverge. É aceitar uma vez por aparelho.

O servidor não tem autenticação. Com `--host 0.0.0.0` ele fica visível para
toda a rede local: use em rede de casa, não em wi-fi público.

## Como funciona

O gerador do 9GAG é totalmente estático — não existe API. O catálogo de 836
templates vive dentro de um bundle JavaScript, e cada template traz a história
do meme, as keywords e a geometria exata das caixas de texto.

O que o 9GAG **não** tem é busca semântica: a busca deles é um `includes()` de
substring sobre nome e keywords. Digitar "meu chefe pediu hora extra de novo"
retorna zero resultados. É essa camada que este projeto acrescenta.

O pipeline tem quatro etapas, e cada uma roda numa cadência diferente:

| Etapa | Quando roda | Custo |
|---|---|---|
| `sync` | quando o 9GAG publica templates novos | grátis |
| `enrich` | só para os templates que mudaram | ~$0,84 pelos 836 |
| `index` | depois de enriquecer | grátis (local) |
| `make` | a cada meme | ~$0,04 |

### sync — acompanhar a curadoria do 9GAG

O 9GAG atualiza o gerador com frequência. O nome do bundle é derivado do
conteúdo, o que dá um jeito barato de detectar mudança:

```
/meme-generator/                  (8 KB)   -> assets/manifest-<build>.js
/assets/manifest-<build>.js       (4 KB)   -> assets/meme-templates-<hash>.js
/assets/meme-templates-<hash>.js  (1.4 MB) -> o catálogo
```

Os dois primeiros passos custam 12 KB e bastam para saber se algo mudou. O
bundle grande só é baixado quando o hash muda de fato.

```console
$ memegen sync
build do 9GAG: assets/manifest-8fb5c86c.js
bundle atual : meme-templates-9ub_3wcz.js
nada mudou desde 2026-09-14T17:07:26+00:00 (836 templates)
```

Quando muda, o diff mostra exatamente o que entrou, saiu ou foi alterado, e só
esses templates voltam para o enriquecimento.

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
  "quando_usar": ["preferência clara", "upgrade de ferramenta", "escolha óbvia"],
  "tom": ["sarcástico", "leve"],
  "slots": [
    {"box_id": "text-0", "papel": "a opção rejeitada", "max_chars": 60},
    {"box_id": "text-1", "papel": "a opção preferida", "max_chars": 60}
  ],
  "nsfw": false
}
```

Roda pela Batch API (metade do preço) e é incremental — o contrato só é refeito
se o `source_hash` do template mudar. Mudar a miniatura não invalida nada;
mudar a descrição, sim.

### make — situação para meme

Uma única chamada escolhe os templates e escreve o texto, porque separar as duas
coisas obriga o seletor a decidir sem saber se o texto vai funcionar.

Três modos de seleção, medidos:

| Modo | Custo/meme (Opus 5) | Precisa de |
|---|---:|---|
| `vetorial` | $0,038 | índice local (`sentence-transformers`, ~2,2 GB) |
| `shortlist` | $0,060 | nada |
| `full` | $0,483 | nada |

`full` manda os 836 contratos (92 mil tokens) no contexto. Com prompt caching
cai para $0,07, mas o TTL padrão é de 5 minutos — em uso pessoal esporádico o
cache está frio quase sempre, então o número relevante é o de cache frio.

O padrão é `auto`: usa busca vetorial se houver índice construído, e cai para a
triagem por LLM se não houver.

O modelo de embedding importa mais do que parece. Medido em 10 situações de
teste, com contratos escritos à mão competindo contra 826 distratores
(`tools/bench_retrieve.py`):

| Modelo | top-5 | top-30 |
|---|---:|---:|
| paraphrase-multilingual-MiniLM-L12-v2 (~120 MB) | 4/10 | 7/10 |
| intfloat/multilingual-e5-large (~2,2 GB), padrão | 9/10 | 10/10 |

top-30 é a métrica que decide, porque é o conjunto que chega ao modelo gerador.
Com o modelo pequeno, em 30% dos casos o meme certo nunca chega — e aí nenhum
modelo de geração consegue acertar. São 10 casos, então é sinal forte, não
prova; vale repetir o benchmark com os contratos reais depois do `enrich`.

### Resolução de saída

Os memes saem com 640 px de largura, que é o teto do que o 9GAG serve — não há
versão maior disponível (`_large`, `@2x` e afins retornam 404; só existe a de
640 e uma miniatura de 240).

Vale saber que `width`/`height` no catálogo **não** são as dimensões da imagem
servida: são as do original. Medido, 779 dos 836 divergem — o Drake diz
1200×1200 e é servido em 640×640. As imagens já vêm redimensionadas para o
canvas, o que é mais uma confirmação de que o canvas é de 640 px.

### Renderização

As coordenadas das caixas não estão em pixels da imagem — estão num canvas de
640 px de largura e altura proporcional. Verificado nos 836 templates: nenhuma
caixa ultrapassa esses limites, e 744 encostam exatamente em x=640. A conversão
para pixels é uma única escala, `largura_da_imagem / 640`.

O renderizador respeita `autoFontSize` (busca binária pelo maior corpo que cabe),
contorno e sombra, `capitalize`, alinhamento e rotação por caixa.

## Instalação

```console
$ pip install -e .
$ export ANTHROPIC_API_KEY=sk-ant-...
```

Opcional, para a busca vetorial local:

```console
$ pip install sentence-transformers
```

Para o visual original, coloque `Impact.ttf` em `fonts/` — veja `fonts/README.md`.

## Primeiro uso

```console
$ memegen sync                 # baixa o catálogo (836 templates)
$ memegen enrich --wait        # gera os contratos — ~$0,84, leva até 1h
$ memegen index                # índice de busca local (opcional)
$ memegen serve                # interface web em localhost:8000
```

Ou pela linha de comando, sem servidor:

```console
$ memegen make "sua situação"
```

Depois disso, manter em dia é só:

```console
$ memegen sync && memegen enrich --wait && memegen index
```

Um cron semanal dá conta.

## Comandos

| Comando | O que faz |
|---|---|
| `memegen sync` | verifica o 9GAG e atualiza o espelho local |
| `memegen enrich` | enriquece os templates cujo contrato está desatualizado |
| `memegen enrich --collect ID` | coleta um lote já enviado |
| `memegen index` | (re)constrói o índice de busca vetorial |
| `memegen make "situação"` | gera memes |
| `memegen status` | estado local |
| `memegen serve` | abre a interface web local |
| `memegen audit` | lista os contratos que merecem revisão manual |

`memegen audit` ordena por risco — templates com muitas caixas, cuja descrição
do 9GAG não traz instrução de uso. São os candidatos a errar o papel dos slots.
Revisar os 40 primeiros cobre a maior parte do risco.

## Modelos

Definidos por variável de ambiente:

- `MEMEGEN_ENRICH_MODEL` (padrão `claude-sonnet-5`) — enriquecimento em lote
- `MEMEGEN_GENERATE_MODEL` (padrão `claude-opus-5`) — escolha e escrita

A escrita é onde a qualidade aparece: é fácil um modelo escolher o template
certo e escrever um texto sem graça. Vale medir antes de economizar aqui.

## O que está validado

| Etapa | Como foi verificada |
|---|---|
| `sync` | executada ao vivo contra o 9GAG; 836 templates baixados e parseados |
| geometria | 836 templates, 1.748 caixas: renderizadas e medidas contra o retângulo declarado. Zero vazamento |
| `render` | 836/836 sem erro (`tools/validate_render.py`) |
| `catalog` | diff incremental, carimbo de `source_hash`, testes unitários |
| `retrieve` | índice e busca reais, com benchmark contra gabarito |
| `enrich` | formato das requisições, parsing e caminhos de erro, com cliente falso |
| `generate` | montagem do prompt, cache, parsing e seleção de modo, com cliente falso |
| `web` | endpoints, cache de render, rejeição de caminho hostil; laço de edição exercitado em navegador real |

O que **não** está validado, e só a API real responde: a qualidade do que o
modelo escreve e a precisão dos contratos que o `enrich` produz. Rode
`memegen audit` depois do primeiro enriquecimento.

```console
$ python -m pytest tests/ -q          # 54 testes
$ python tools/download_all.py        # todas as imagens (37 MB)
$ python tools/validate_render.py     # renderiza e mede os 836
$ python tools/bench_retrieve.py      # qualidade da busca
```

## Dados

Nada em `data/` é versionado. O catálogo é espelho de conteúdo público do 9GAG,
os contratos são derivados dele, e ambos se reconstroem com `sync` + `enrich`.
