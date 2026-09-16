# Contexto para sessões futuras

Projeto pessoal do André (`antigerme`). Conversa conduzida **em português**;
código, commits e comentários em português também. Mantenha assim.

Branch de trabalho: `claude/eager-darwin-7tbcru`.

## O que é

Gerador de memes com IA sobre a curadoria de templates do 9GAG. O usuário
descreve uma situação, a IA escolhe os memes que se aplicam e escreve o texto de
cada caixa.

O diagnóstico que motivou o projeto: o 9GAG tem 836 templates com história e
keywords, mas a busca deles é `includes()` de substring sobre nome e keywords.
Digitar "meu chefe pediu hora extra de novo" retorna zero. A camada que falta é
semântica, e é o que este projeto acrescenta.

## Restrições que não podem ser quebradas

Foram pedidas explicitamente pelo usuário, depois de uma versão anterior com
FastAPI, Pillow, httpx, pydantic e sentence-transformers ter sido descartada:

1. **Arquivo único.** Todo o programa em `memegen.py`.
2. **Zero dependências.** Só biblioteca padrão. Sem pip, sem venv.
3. **Python 3.6 a 3.14.** O servidor mais velho dele é 3.6.8; o laptop, 3.14.7.

Dois testes guardam isso e falham se for violado: um verifica que nada fora da
stdlib é importado, outro que não aparece sintaxe posterior ao 3.6. Rode também
`vermin --target=3.6 memegen.py` se tiver disponível.

Isso aperta dos dois lados: nada de dataclasses, `from __future__ import
annotations`, walrus, f-string com `=`, anotação com tipo embutido
parametrizado (todos posteriores ao 3.6); e nada de `ssl.wrap_socket`,
`capture_output`, `unlink(missing_ok=)`, `datetime.utcnow()` (removidos ou
obsoletos nas versões novas).

## Descobertas medidas (não refazer da cabeça)

**Canvas de 640 px.** As coordenadas das caixas de texto não estão em pixels da
imagem: estão num canvas de 640 de largura, altura proporcional. Verificado nos
836 templates — nenhuma caixa ultrapassa, e 744 encostam exatamente em x=640.

**Dimensão declarada ≠ servida.** `width`/`height` no catálogo são do original;
o 9GAG serve tudo já redimensionado para o canvas. Medido: 779 dos 836 divergem
(Drake diz 1200×1200, é servido em 640×640). A escala de render sai da largura
**real** do arquivo. Usar a declarada erraria em quase todos. 640 px é o teto —
`_large`, `@2x` e variantes dão 404.

**A ordem das caixas não é a ordem semântica.** Em `distracted_boyfriend` a
sequência é mulher-de-vermelho, namorado, namorada. Só 40 dos 836 têm a
descrição do 9GAG explicando os papéis. É a razão de existir o enriquecimento.

**Gemini 3.x raciocina por padrão.** `thinking_level` vem em `medium` e
`max_output_tokens` limita pensamento + saída **somados**. Sem enviar os dois, a
triagem sobre 836 templates estourava quatro timeouts de 90 s. Com
`thinking_level: low` ela roda em 3,7 s e o `total_thought_tokens` volta zero —
confirmado no debug, então o `generation_config` é mesmo aceito.

**As mensagens de cota do Google são ambíguas.** `RESOURCE_EXHAUSTED` serve para
limite por minuto e para cota diária, e a de cota diária também diz "Please
retry in 20.8s" e "check your plan and billing details". Não tente classificar
pela mensagem — já tentei e o teste derrubou. Por isso o 429 insiste só uma vez
(`TENTATIVAS_429`), enquanto o 5xx usa as quatro.

**A busca vetorial foi testada e descartada.** Indexar a descrição do 9GAG casa
por assunto de superfície, não por função ("dormir cedo ou terminar a série"
trazia Alarm Clock em vez de Two Buttons). Com contratos bons e
`multilingual-e5-large` chegava a top-30 10/10, mas o modelo tem 2,2 GB e virou
dependência proibida na reescrita. Hoje a triagem é por LLM.

## Estado em 16/09/2026

- Catálogo: 836 templates sincronizados
- Contratos: **836 de 836, completo.** O enriquecimento acabou — só volta a
  rodar quando o 9GAG publicar templates novos (o `sync` detecta)
- Provedor em uso: Gemini (cota gratuita). A API da Anthropic é pré-paga, sem
  tier gratuito, e a conta dele não tem crédito
- Cota gratuita do Gemini, medida no painel do AI Studio:
  - `gemini-3.5-flash-lite` (enriquecimento e triagem): 500/dia, 15/min
  - `gemini-3.8-flash` (geração): **20/dia**, 5/min — aperta na hora de avaliar
    a qualidade dos memes

Validado rodando de verdade no Fedora 44 com Python 3.14.7: os 96 testes, o
`sync`, o enriquecimento completo dos 836 via Gemini, a qualidade dos contratos
(conferida no `distracted_boyfriend`, o caso mais difícil — o modelo acertou os
três papéis) e a retomada após interrupção, crash e cota esgotada.

## Em aberto

1. **O usuário nunca viu um meme gerado.** É o passo 6, o único que falta, e a
   pergunta que decide o projeto: o texto tem graça? LLM escreve legenda de
   meme sem graça por padrão. Atenção à cota: só 20 gerações por dia.
2. **Só falta a geração rodar.** A triagem foi confirmada funcionando em
   16/09: 3,7 s para peneirar os 836 (108 mil caracteres) e devolver 26
   candidatos sensatos. A geração parou em 429 — a cota de 20/dia do
   `gemini-3.8-flash` já tinha acabado. Não há bug conhecido nesse caminho,
   só falta cota.
3. **Bug no modo `full`:** `400: Request contains an invalid argument` no
   Gemini. O enriquecimento fez 494 chamadas sem um 400, então é específico
   desse modo — suspeita de tamanho (manda ~220 KB de contratos no
   `system_instruction`) ou complexidade do schema. Precisa de cota para
   reproduzir.
4. Fonte Impact não existe no Fedora; o canvas cai para alternativa. Não foi
   avaliado se o visual incomoda.

## Ferramenta de diagnóstico

`--debug` (ou `MEMEGEN_DEBUG=1`) imprime cada chamada ao modelo: etapa, carga
enviada, `generation_config`, tentativa, tempo e resposta. Use antes de
formular hipótese sobre lentidão ou erro de API — foi criado exatamente porque
estávamos adivinhando.

## Como ele trabalha

Pediu passo a passo, um comando por vez, esperando a saída antes de seguir.
Respeite esse ritmo — não despeje a sequência inteira.

E ele corta remendo. Quando a cota do modelo de triagem acabou, eu fiz o
programa cair para o catálogo inteiro por baixo dos panos; ele mandou reverter,
com razão — o fallback trocou um "429, cota excedida, limite 500/dia" legível
por um "400 invalid argument" confuso vindo de outro caminho quebrado. Se a
causa é cota, a mensagem tem que dizer cota.

## Onde está o resto do porquê

As mensagens de commit têm 243 linhas de corpo explicando as decisões e o que
as motivou — inclusive os erros cometidos e como apareceram. `git log` é a
fonte, não um changelog decorativo. O README cobre arquitetura e uso.
