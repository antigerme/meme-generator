# Contexto para sessões futuras

Projeto pessoal do André (`antigerme`). Conversa conduzida **em português**;
código, commits e comentários em português também. Mantenha assim.

Branch de trabalho: `main`.

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

## O caminho completo funciona (16/09/2026)

Situação → triagem → geração → imagem, ponta a ponta, com o usuário na frente.
A triagem leva 3,7 s para peneirar os 836 contratos (108 mil caracteres) e
devolver ~26 candidatos sensatos; a geração escreve o texto; o canvas desenha.

O primeiro meme aprovado saiu com `gemini-3.5-flash-lite` na geração — o modelo
**mais fraco** da família, usado porque a cota do `3.8-flash` (20/dia) tinha
acabado. Sinal de que o peso está nos contratos, não na potência do modelo que
escreve. Falta comparar com o `3.8-flash` para ver se a diferença justifica o
limite apertado.

## Em produção desde 16/09/2026

VM RHEL 8.10 na Oracle Cloud, Python 3.6.8 — a máquina que motiva a restrição
de versão. As 118 passam lá. **Desde 18/10 a VM é produção: fica ligada o tempo
todo** (antes ele só a ligava para testar). O `enable` do systemd faz o serviço
subir sozinho no boot.

    navegador --HTTPS--> Cloudflare --HTTP--> Apache :80 --> memegen 127.0.0.1:8000

- `/opt/memegen`, código e `dados/`, dono `opc`. Ele recusou usuário de sistema;
  `NoNewPrivileges=yes` fecha a escalada por sudo que o `opc` tem sem senha
- `/etc/memegen.env` 0600 root. **Desde 18/10 a VM roda no provedor Anthropic**
  (chave da Anthropic e os três `MEMEGEN_*_MODEL` apontando para a geração 5.5:
  enriquecer `claude-sonnet-5-5`, triar `claude-haiku-5-5`, gerar
  `claude-opus-5-5`). As linhas do Gemini ficaram comentadas. Os padrões dentro
  do código (`PADROES`) também apontam para a geração 5.5 desde 18/10; as
  variáveis da VM continuam e valem sobre eles
- `memegen.felicio.com.br`, **sem autenticação**, por decisão dele. **Essa decisão
  caiu em duas premissas**: foi tomada quando a VM só ligava para testar e a API
  era o Gemini gratuito (pior caso: gastar cota). Hoje a VM está sempre no ar e
  cada geração é paga na Anthropic. Pendentes de decisão dele: teto de gasto
  mensal no Console da Anthropic, Cloudflare Access (ou outro controle de
  acesso), e revogar a chave do Gemini, que apareceu em texto claro numa
  conversa
- (Até 17/10 a geração na VM usava `gemini-3.5-flash-lite`, 500/dia em vez das 20
  do `3.8-flash`.)
- **Validado de ponta a ponta no ar:** ele abriu o domínio e gerou memes. Toda
  a corrente — Cloudflare, Apache, serviço, triagem, geração, canvas —
  funcionando junta
- O README tem os arquivos e o porquê de cada diretiva

Três coisas descobertas montando isso:

**As imagens têm que viajar com os contratos.** `_resumo` mede a largura real
lendo o cabeçalho do JPEG local e, sem o arquivo, cai para a dimensão declarada
— a que diverge em 779 dos 836. A imagem só é baixada quando o navegador pede
`/template/<id>.jpg`, depois da sugestão já ter sido respondida. Numa instalação
nova sem as imagens, cada template erraria a primeira renderização e acertaria
a segunda. Não reproduz.

**`mod_pagespeed` quebraria a página.** O Apache dele carrega o módulo, que
reescreve HTML/CSS/JS em voo. Uma página única com `<style>` e `<script>`
embutidos e render em canvas não sobrevive a isso. O vhost tem `ModPagespeed
off`.

**`PYTHONUNBUFFERED=1` no unit.** Sob o systemd a saída vai para um pipe e o
Python bufferiza em bloco: sem isso um erro fica invisível no `journalctl` até
o buffer encher. A mesma armadilha nos pegou duas vezes na mesma sessão lendo
saída de teste por pipe.

O cache em memória compara o mtime de `catalogo.json` e `contratos.json` a cada
leitura, então `sync` e `enrich` não exigem restart do serviço.

**18/10: apagaram `dados/` e o timer só recriou metade.** Catálogo e imagens
voltaram, e o `enrich` enviou um lote de 836 na Batch API (Anthropic) e saiu. Os
contratos só entram com `enrich --collect`, e o id do lote só existia no
journal: a rodada seguinte, com contratos ainda vazios, reenviaria os 836 e
cobraria em dobro. Corrigido: o id vai para `dados/lote.json` ao enviar, e todo
`enrich` começa por ele (andando: não envia; terminado: coleta; 404: descarta
dizendo que descartou; qualquer outro erro sobe, porque na dúvida não se
reenvia). O contrato entra na próxima rodada do `enrich` **depois** de o lote
terminar, não quando ele termina: com timer diário, pode ser o dia seguinte. A página avisa isso em vez de dizer "quando terminar". A coleta usa o hash do momento do **envio**, não o de agora: coletado
um dia depois, o template pode ter mudado ou sumido. Limite conhecido: `rm -rf
dados/*` leva o `lote.json` junto.

**Validado na VM real, nos dois ramos novos.** 1ª rodada: envia o lote de 836 e
grava `lote.json` (36 KB, os 836 hashes). 2ª rodada, 2 minutos depois: `ainda em
andamento … 0 prontos, 836 na fila. Nada novo foi enviado` — sem reenvio. 3ª
rodada, ~14 min após o envio: `terminou; coletando`, `contratos gravados: 836
ok, 0 com erro`, registro apagado, `contratos.json` com dono `opc`. Antes do
conserto estar na VM, o mesmo vaivém de "apagar `dados/` e iniciar o serviço"
enviou **quatro** lotes de 836 em 12 minutos (03:53, 04:00, 04:01, 04:04). Fica a
lição: o registro mora dentro de `dados/` (o unit só deixa escrever ali), então
apagar `dados/` apaga a memória do lote e cada novo início envia outro. Ele
disse que custo não é problema (tem US$ 100/mês de crédito).

Conferência dos 836 contratos escritos pelo `claude-sonnet-5-5`: todos os
modelos iguais, nenhum template sem contrato, **0 contratos cujos slots não
batem com as caixas**. `distracted_boyfriend`, o caso difícil, saiu certo: caixa
1 a tentação, 2 quem se distrai, 3 o que está sendo ignorado. Um lote de 836
terminou em menos de 15 min (medido uma vez; a API só garante até 24 h).

Medido no mesmo episódio: o enriquecimento com `claude-sonnet-5-5` e
`max_tokens` 2000 voltou **836 de 836 válidos**, 0 com erro. Isso aposenta o
risco de truncamento só no enriquecimento.

**A triagem em `haiku-5-5` estourava o teto, e a primeira geração real falhou.**
A tela disse `na etapa 'triar', o modelo nao devolveu JSON valido:` com nada
depois dos dois pontos. No Claude o pensamento sai do mesmo `max_tokens` da
resposta, e nos modelos 5.5 ele vem ligado por padrão (Haiku 5.5 e Opus 5.5 em
esforço `medium`, Sonnet 5.5 em `high`; confirmado na página de modelos da
Anthropic). Uma sonda na VM chamou a triagem uma vez e **passou, por 33
tokens**: `output_tokens` 1467 de um teto de 1500, dos quais 1155 só de
pensamento (resposta de 643 caracteres). O pensamento varia entre chamadas, então
uma que pense um pouco mais fica sem bloco de texto. Não capturei a resposta da
falha da tela: é forte evidência, não prova da causa daquela chamada.

Conserto: o teto enviado ao Claude ganha `FOLGA_PENSAMENTO_CLAUDE` (12000, só
limite, só se paga o que se usa; a mesma folga do Gemini em `medium`); a
resposta cortada por `max_tokens` e a resposta sem texto agora dizem a causa, com
`stop_reason` e a contagem de pensamento; o `--debug` imprime `stop_reason`,
saída e pensamento. O enriquecimento em lote (2000) não mudou: foi medido,
836 de 836. Não mexi em `effort`: o padrão de triagem agora é o Haiku 5.5, que
aceita o parâmetro, mas um modelo mais antigo posto numa variável de ambiente
(Haiku 4.5, por exemplo) o rejeita, então um botão de esforço ainda precisaria
de guarda por modelo.

**Medido na página, já com o conserto:** triagem `48+1121 tok` (de novo a 379 do
teto antigo de 1500, depois dos 1467 da sonda) e geração `47+857 tok`, **433 de
pensamento** no `opus-5-5` em `medium` — longe dos 4000 antigos. O `48` e o `47`
de entrada não contam o cache de prompt (o catálogo da triagem sai quase todo de
lá); não é a contagem zerada de antes. Na mesma captura: o botão **Copiar**
habilitado prova contexto seguro (HTTPS do Cloudflare); **Compartilhar** cinza é
esperado no Linux desktop e só se testa no celular.

O `memegen-sync.timer` já está instalado e habilitado na VM: roda `sync` e
depois `enrich --pausa 6` uma vez por dia, com `Persistent=true` para não
perder o disparo enquanto a máquina está desligada. Custa nada quando não há
novidade — o `enrich` checa os pendentes antes de tocar na API e sai sem
gastar cota.

## Em aberto

1. **Comparar `opus-5-5` e `sonnet-5-5` na geração** (custo, tempo, qualidade),
   agora que a VM roda no Claude. A comparação antiga entre `3.8-flash` e o lite
   do Gemini ficou obsoleta com a troca de provedor.
2. **Bug no modo `full` (era do Gemini; não testado no Claude):**
   `400: Request contains an invalid argument` no Gemini. O enriquecimento fez 494 chamadas sem um 400, então é específico
   desse modo — suspeita de tamanho (manda ~220 KB de contratos no
   `system_instruction`) ou complexidade do schema. Precisa de cota para
   reproduzir.
3. **Comando para só baixar as imagens.** `garantir_imagens` existe mas só o
   `enrich` chama, e só para os pendentes. Com os contratos completos não há
   pendente e nada é baixado — foi por isso que o deploy precisou de `rsync`
   das imagens. Expor na CLI resolve.
4. **Timeout de 100s do Cloudflare** (erro 524, não configurável fora do
   Enterprise). O caminho normal passa longe, a triagem mede 3,7s, mas um retry
   com backoff estoura e troca o "429, cota excedida" por um 524 confuso. Se
   encostar, a saída é `/api/gerar` responder na hora e o resultado ser
   consultado depois.
5. **Esforço por etapa.** A triagem em `haiku-5-5` pensa ~1100 tokens num
   trabalho de classificação; um botão de `effort` baixaria isso, mas exige
   guarda por modelo (Haiku 4.5, que ainda se pode pôr numa variável de ambiente,
   rejeita o parâmetro; os padrões do código já são 5.5). Só
   vale depois de decidir se o custo/tempo da triagem incomoda.
6. **Template com texto já impresso na imagem.** No "How About No Bear" a
   legenda nova cai por cima de um "HOW ABOUT NO" que vem na própria imagem do
   9GAG. **Conferido baixando o original** (640x384): o texto está impresso na
   base da foto, perto de y 310-360. A caixa `text-1` do catálogo deles (y=320,
   h=64, centralizada) cobre exatamente esse trecho, então a sobreposição vem
   da geometria do próprio 9GAG, não do nosso render (o editor deles eu não vi).
   Contorno hoje: apagar o campo de baixo no cartão, porque `desenhar` pula
   caixa vazia. Quantos dos 836 têm texto impresso é desconhecido: só olhando as
   imagens. Se um dia valer o custo, um campo no contrato (a imagem já traz
   texto?) exige reenriquecer; uma lista curta à mão dos casos conhecidos é o
   caminho barato.
7. **RESOLVIDO.** Imagens que falhavam de forma aleatória na aba Templates (18/10).
   Subi a fila de conexões do servidor de 5 (padrão do `socketserver`) para 128:
   **não há teto de threads para subir**, o `ThreadingMixIn` cria uma por conexão.
   Medido localmente com as 836 imagens e conexões novas de uma vez: a fila de 5 já
   dava um pedido de 1,24 s com 30 conexões e p95 de 1,23 s com 150; com 128, p95
   de 0,09 s. **Zero erros nos dois**: a fila pequena explica *lentidão* em rajada,
   não *falha*. **Ele confirmou, depois de `pull` + restart na VM, que as imagens
   passaram a carregar** (a causa é inferida, sem medição antes/depois no ar:
   fila pequena em rajada é a explicação mais simples). Também descartado
   medindo: não é template específico (836 de 836 baixam, nenhum id é recusado
   pelo `NOME_SEGURO`). A evidência decisiva, se persistir, está nos logs do
   Apache (`/var/log/httpd/memegen_access.log` para ver o status dos pedidos que
   falham, `memegen_error.log` para a causa: reset, timeout, conexão recusada).
   Não adicionei timeout de conexão ociosa no servidor de propósito: o Apache
   reaproveita conexões com o backend, e um backend que fecha conexão ociosa
   cria a corrida clássica do 502 intermitente.
8. Fonte Impact não existe no Fedora; o canvas cai para alternativa. Não foi
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
