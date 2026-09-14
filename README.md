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
| `vetorial` | $0,038 | índice local (`sentence-transformers`) |
| `shortlist` | $0,060 | nada |
| `full` | $0,483 | nada |

`full` manda os 836 contratos (92 mil tokens) no contexto. Com prompt caching
cai para $0,07, mas o TTL padrão é de 5 minutos — em uso pessoal esporádico o
cache está frio quase sempre, então o número relevante é o de cache frio.

O padrão é `auto`: usa busca vetorial se houver índice construído, e cai para a
triagem por LLM se não houver.

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

## Dados

Nada em `data/` é versionado. O catálogo é espelho de conteúdo público do 9GAG,
os contratos são derivados dele, e ambos se reconstroem com `sync` + `enrich`.
