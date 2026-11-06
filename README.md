# 📌 Pinterest Bot: divulgação autônoma de produtos digitais

Automação serverless que roda **100% no GitHub Actions**. Ela divulga diariamente no Pinterest os produtos que seus agentes de IA criam no **Teachers Pay Teachers, Notion Marketplace, Etsy e TES.com**.

A cada execução o bot:

1. **Lê** os produtos em `data/products.json` e, se configurado, em feeds remotos JSON/RSS.
2. **Escolhe** de forma aleatória e ponderada quais produtos divulgar. Nunca repete o mesmo produto em sequência e, com o tempo, prioriza os que têm mais visualizações e vendas.
3. **Usa a copy da fila** (título SEO, descrição persuasiva, hashtags e alt text). Quem gera essa fila é o **seu PC, com o Claude Code da sua assinatura**, sem API paga (veja [Fila de copies](#-fila-de-copies-gerada-no-seu-pc)). O inglês segue a plataforma: 🇺🇸 americano para TPT, Notion e Etsy, 🇬🇧 britânico para TES.
4. **Cria o Pin** vertical de 1000×1500 px, com 3 a 5 variações visuais por produto (5 templates × 8 paletas).
5. **Publica** pelo canal disponível (webhook Make/Zapier ou Pinterest API v5), com fallback automático.
6. **Grava** o histórico em `data/history.json` com um commit automático, para nunca republicar a mesma variação.

---

## 🗂 Estrutura

```
.github/workflows/pinterest_bot.yml   # agendamento (4x/dia) + persistência do estado
src/
  config.py               # env vars, perfis por plataforma (idioma, tom, badges, CTAs)
  ingestion.py            # leitura de products.json (+ feeds JSON/RSS opcionais)
  scheduler.py            # rotação aleatória ponderada por desempenho + ciclos de variações
  seo_copywriter.py       # copy SEO com Claude (cache + fallback por template)
  image_generator.py      # 5 templates Pillow: bold_header, full_bleed, minimal, split, notebook
  image_host.py           # URL pública da imagem (branch pin-assets do GitHub ou imgbb)
  pinterest_publisher.py  # canais webhook / api / dryrun com fallback e tratamento de 401/429
  history.py              # estado persistente
  main.py                 # orquestrador
scripts/download_fonts.py # baixa as fontes Google Fonts (OFL) usadas nos templates
scripts/local_refill.py   # (seu PC) gera a fila de copies com Claude Code quando ela está acabando
scripts/setup_windows_task.ps1  # (seu PC) agenda o refill no Agendador de Tarefas do Windows
templates/fonts, templates/assets
data/products.json        # ← seus agentes escrevem aqui
data/stats.json           # ← (opcional) views/vendas por produto para priorização
data/stores.json          # ← links das suas lojas (sincronização automática)
PRODUTOS.txt              # ← bloco de notas: "Nome | link | observações" de cada produto publicado
data/products.example.json  # exemplos de formato (não são publicados)
scripts/add_links.py / adicionar_links.bat  # cola links dos produtos que você publicou
src/store_sync.py         # varre as lojas, liga pendentes e importa o catálogo
data/boards.json          # ← (opcional) board do Pinterest por plataforma
data/history.json         # estado (gerado pelo bot)
data/copy_cache.json      # fila de copies (escrita SÓ pelo seu PC)
data/copy_cache_actions.json  # copies geradas no Actions (só com EMPTY_QUEUE_POLICY=api)
```

---

## 🎨 Artes criadas pelo Claude

Para cada variação, o refill do PC pede ao Claude um **design de Pin em HTML/CSS com ilustrações SVG**. Cada variação recebe um conceito diferente, seguindo os formatos que mais engajam no Pinterest:
- pôster tipográfico;
- listicle ou checklist;
- problema → solução;
- prévia estilizada do material;
- flat-lay de mesa;
- mural de post-its;
- capa editorial;
- tema sazonal.

**Como a arte vira imagem e chega ao Pin:**
- O navegador (Chrome ou Edge) converte o HTML em imagem 1000×1500. Isso já é feito no PC para validar: design quebrado é descartado na hora.
- O HTML fica em `data/designs/<produto>/c<ciclo>_v<n>.html`. O GitHub Actions só renderiza esse HTML na hora de publicar, porque o Chrome já vem no runner.
- A capa ou o mockup do produto, quando existe, entra como elemento da arte. Sem imagem, a arte é 100% criada.
- Regras de honestidade no prompt: nada de avaliações, "best seller" ou números inventados. Só usa o texto da copy aprovada.
- Sem arte disponível, o bot usa os templates Pillow como reserva.
- Para desligar as artes: `AI_DESIGNS=false` no `.env` do PC.
- As prévias ficam em `output/previews/`.

## 🖥 Fila de copies gerada no seu PC

```
 Seu PC (Claude Code, sua assinatura)            GitHub Actions (4x/dia)
 ───────────────────────────────────             ───────────────────────────────
 a cada 4h e ao ligar:                           seleciona produtos (prioriza quem
   git pull                                      tem copy pronta na fila)
   fila < 5 dias ou produto novo?  ──não──► sai  gera imagem com Pillow (sem IA)
        │ sim                                    publica no Pinterest
   claude -p (headless, JSON validado)           grava data/history.json
   grava data/copy_cache.json
   git commit + push  ───────────────────────►   lê a fila no próximo run
```

- **Sem custo de API**: o `claude -p` usa o login do Claude Code no seu PC. Cada chamada gera todas as variações de um produto de uma vez.
- **Só age quando precisa**: o refill mede quantos dias de Pins prontos restam (`REFILL_MIN_DAYS`, padrão 5) e só chama o Claude abaixo desse limiar, ou quando entra produto novo. Ele também adianta o próximo ciclo quando o atual está acabando.
- **Limite da assinatura**: se o limite for atingido, o script para sem erro e continua na próxima execução. No máximo `REFILL_MAX_PRODUCTS` (15) produtos são processados por rodada.
- **Sem conflitos de Git**: o PC é o único que escreve `copy_cache.json` e o Actions é o único que escreve `history.json`.
- **Se o PC ficar desligado e a fila zerar** (`EMPTY_QUEUE_POLICY` no GitHub):
  - `template` (padrão): copy simples por template, sem IA. O bot nunca para.
  - `skip`: publica só produtos com copy pronta. Se não houver nenhum, a execução passa em branco.
  - `api`: gera com a API Anthropic como reserva. Requer o Secret `ANTHROPIC_API_KEY`.

### Instalar no PC (uma vez)
1. Clone o repositório no PC (ou use esta pasta depois de dar `git push`). Faça um `git push` manual uma vez para salvar as credenciais no Git Credential Manager.
2. `pip install -r requirements.txt`
3. Confirme que o Claude Code está logado na sua conta. O script encontra sozinho o `claude` no PATH ou o binário que vem com a extensão do VS Code. Se não encontrar, defina `CLAUDE_BIN` no `.env`.
4. Teste: `python scripts/local_refill.py --status` (mostra a fila) e depois `python scripts/local_refill.py --force`.
5. Agende: `powershell -ExecutionPolicy Bypass -File scripts\setup_windows_task.ps1`
   - A tarefa roda a cada 4 h e 5 minutos após o logon. Se o PC estava desligado no horário, roda assim que ligar.
   - O log fica em `logs/refill.log`.

---

## 🔌 Estratégia de publicação: por que webhook primeiro

Resultado da pesquisa sobre as opções:

| Opção | Aprovação | Estabilidade | Veredito |
|---|---|---|---|
| **Make.com / Zapier / IFTTT** (módulo oficial "Create a Pin") | **Nenhuma.** Você só conecta sua conta Pinterest; o app deles já é aprovado pelo Pinterest | Alta | ✅ **Canal principal (`webhook`)** |
| **Pinterest API v5 oficial** (`POST /v5/pins`) | O app próprio começa em *Trial*: os Pins criados ficam restritos/sandbox. Pins públicos exigem *Standard access* (revisão do Pinterest, com demo em vídeo) | Alta | ✅ **Canal secundário (`api`)**, já implementado com refresh automático de token |
| **n8n** | Não tem nó oficial do Pinterest. Usa HTTP Request com o seu próprio app, o que cai na mesma aprovação da API | Alta | ⚠️ Funciona como receptor do webhook se você já tiver o app aprovado |
| **Wrappers não oficiais** (ex.: `py3-pinterest`, automação de login/browser) | Nenhuma | **Baixa:** quebram a cada mudança do site, exigem senha/2FA e violam os Termos de Uso | ❌ **Não implementado.** O risco é perder a conta inteira, junto com todo o tráfego acumulado |

Os canais ficam configurados em `PUBLISH_CHANNELS` (padrão `webhook,api`): o bot tenta o primeiro e, se ele falhar, passa para o próximo.

- **401** (token expirado): o bot tenta renovar com o refresh token. Se não conseguir, desativa o canal e registra `::error::`.
- **429** (rate limit): o canal é desativado até a próxima execução e o próximo canal assume.
- **Falha em todos os canais**: o Pin **não** é marcado como publicado (volta para a fila), o job fica vermelho e o GitHub te avisa por e-mail.

---

## 🚀 Configuração passo a passo

### 1. Criar o repositório
Suba esta pasta para um repositório no GitHub.
- **Repositório público**: as imagens podem ser servidas pelo próprio GitHub (`IMAGE_HOST=github`, padrão, sem chave extra), e o GitHub Actions fica ilimitado.
- **Repositório privado**: use `IMAGE_HOST=imgbb` (chave gratuita em https://api.imgbb.com/).

### 2. Configurar o cenário no Make.com (canal `webhook`, recomendado)
1. Crie um cenário: **Webhooks → Custom webhook** e copie a URL gerada.
2. Clique em *Redetermine data structure* e rode o bot uma vez em modo dry-run com o webhook. Outra opção é enviar o JSON de exemplo abaixo.
3. Adicione o módulo **Pinterest → Create a Pin** e conecte sua conta Pinterest:
   - **Board**: `{{board_id}}`. Outra opção é colocar um *Router* com filtro por `{{platform}}` e um board fixo em cada rota.
   - **Image URL**: `{{image_url}}`
   - **Title**: `{{title}}` · **Description**: `{{description}}` · **Link**: `{{link}}` · **Alt text**: `{{alt_text}}`
4. *(Opcional)* Adicione **Webhooks → Webhook response** com o corpo `{"id": "{{ID do Pin criado}}"}`. Assim o ID do Pin aparece no histórico.
5. *(Opcional, segurança)* Adicione um filtro logo após o webhook: `X-Webhook-Secret` = o valor do Secret `PIN_WEBHOOK_SECRET`.
6. Ative o cenário (*Scheduling: Immediately*).

Payload enviado pelo bot:
```json
{
  "board_id": "1234567890", "title": "...", "description": "... #hashtags", "link": "https://...",
  "alt_text": "...", "image_url": "https://raw.githubusercontent.com/.../pins/x.jpg",
  "platform": "tpt", "locale": "en-US", "product_id": "...", "variant_id": "id:c0:v2",
  "template": "split", "keywords": ["..."]
}
```

> Volume: 4 execuções × 2 Pins × 30 dias ≈ 240 Pins/mês. Isso dá cerca de 500 a 750 operações no Make, dentro do plano gratuito (1.000 ops/mês).

### 3. *(Opcional)* Pinterest API v5 (canal `api`)
1. Crie um app em https://developers.pinterest.com/apps/ com os escopos `boards:read`, `pins:read` e `pins:write`.
2. Gere o access token e o **refresh token** pelo fluxo OAuth.
3. O bot renova o access token sozinho a cada execução. O refresh token dura cerca de 1 ano, e o bot avisa no log se o Pinterest emitir um novo.
4. Para Pins públicos, solicite **Standard access** no painel do app.

### 4. Descobrir os IDs dos boards
O ID aparece no Make ao listar os boards. Se você usar a API: `GET https://api.pinterest.com/v5/boards`.
Recomendação: um board por nicho, com nome rico em palavra-chave (ex.: *"4th Grade Math Activities"*, *"KS2 English Resources"*, *"Aesthetic Notion Templates"*, *"Digital Planners"*).

### 5. Cadastrar os Secrets
Em **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Obrigatório | Descrição |
|---|---|---|
| `ANTHROPIC_API_KEY` | Opcional | Só se `EMPTY_QUEUE_POLICY=api` (reserva paga quando a fila do PC acaba) |
| `PIN_WEBHOOK_URL` | ✅ (canal webhook) | URL do Custom Webhook do Make/Zapier |
| `PIN_WEBHOOK_SECRET` | Opcional | Enviado no header `X-Webhook-Secret` para validar a origem no Make |
| `PINTEREST_BOARD_MAP` | ✅ (um dos dois) | JSON com o board por plataforma: `{"tpt":"111","tes":"222","notion":"333","etsy":"444","default":"111"}` |
| `PINTEREST_BOARD_ID` | ✅ (um dos dois) | Board único padrão, caso não use o mapa |
| `IMGBB_API_KEY` | Se `IMAGE_HOST=imgbb` | Chave gratuita do imgbb (obrigatória em repositório privado) |
| `PINTEREST_ACCESS_TOKEN` | Canal api | Access token OAuth do seu app |
| `PINTEREST_REFRESH_TOKEN` | Canal api | Refresh token (renovação automática) |
| `PINTEREST_APP_ID` | Canal api | App ID (client_id) |
| `PINTEREST_APP_SECRET` | Canal api | App secret (client_secret) |

O `GITHUB_TOKEN` é fornecido automaticamente pelo GitHub e não precisa ser criado.

**Variables** (opcionais, em **Settings → Secrets and variables → Actions → Variables**):

| Variable | Padrão | Descrição |
|---|---|---|
| `PUBLISH_CHANNELS` | `webhook,api` | Ordem dos canais (`webhook`, `api`, `dryrun`) |
| `IMAGE_HOST` | `github` | `github` (repo público), `imgbb` ou `none` |
| `PINS_PER_RUN` | `2` | Pins por execução (4 execuções/dia → 8 Pins/dia) |
| `JITTER_MAX_MINUTES` | `10` | Atraso aleatório no início, para parecer orgânico |
| `EMPTY_QUEUE_POLICY` | `template` | Fila de copies vazia: `template`, `skip` ou `api` |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Modelo da reserva via API |
| `PRODUCT_FEED_URLS` | — | Feeds extras (JSON ou RSS) separados por vírgula |

Ajustes finos via env (já têm padrões bons): `VARIANTS_MIN`/`VARIANTS_MAX` (3/5), `PRODUCT_COOLDOWN_HOURS` (18), `AVOID_LAST_N_PRODUCTS` (3), `NEW_PRODUCT_BOOST_DAYS` (10), `DELAY_MIN_SECONDS`/`DELAY_MAX_SECONDS` (45/150).

### 6. Liberar escrita do workflow
Em **Settings → Actions → General → Workflow permissions**, marque **Read and write permissions**.

### 7. Testar
Em **Actions → Pinterest Bot → Run workflow**, marque **dry_run** e rode. Depois baixe o artifact `pins-<id>` e confira as imagens. Quando estiver tudo certo, rode sem dry_run.

---

## 🧾 Como seus agentes registram produtos

### Fluxo com publicação manual nas lojas
```
Agente cria o produto ──► products.json com "status": "pending" (sem link)
                           │   o refill do PC já gera as copies com antecedência
Você publica na loja ─────► dois cliques em adicionar_links.bat, cola o link
                           │   valida a página, limpa ?ref=/click_key, ativa, faz push
Bot (GitHub Actions) ─────► o produto entra na rotação na próxima execução
```

**O que o agente grava** (sem `url`):
```json
{
  "title": "Halloween Reading Comprehension Passages - 3rd Grade",
  "description": "Five Halloween-themed reading passages with...",
  "platform": "tpt",
  "status": "pending",
  "image_url": "data/mockups/halloween-reading.png",
  "created_at": "2026-10-30T09:00:00Z",
  "tags": ["halloween reading", "3rd grade reading comprehension"]
}
```
- **Mockup:** o agente pode salvar a imagem em `data/mockups/` e apontar para ela com um caminho relativo, ou usar uma URL pública.
- **Imagem automática:** se não houver mockup, o `add_links` tenta pegar a imagem principal do anúncio. Isso funciona no TES e no Notion. **TPT e Etsy bloqueiam a leitura automática** (o link é aceito mesmo assim), então para essas lojas o mockup deve vir do agente.
- **`id`:** é opcional. Se faltar, é gerado a partir da plataforma e do título e continua o mesmo depois que o link é adicionado.

**Depois de publicar na loja:**
- dois cliques em `adicionar_links.bat` (ou `python scripts/add_links.py`): lista os pendentes, você cola os links e pode dar Enter para pular algum;
- `python scripts/add_links.py --list`: só mostra os pendentes;
- `python scripts/add_links.py --set <id> <url>`: define um link direto.

O script:
- recusa links de página inexistente (404);
- avisa se o domínio não bate com a plataforma (ex.: link do Etsy em produto TPT);
- remove os parâmetros de rastreio do link.

### 📝 Bloco de notas `PRODUTOS.txt` (o jeito mais simples)
Publicou um produto? Abra o `PRODUTOS.txt` na pasta do projeto e adicione uma linha:
```
Nome do produto | link | observações (opcional)
```
- **Só o link já basta.** Sem nome, o bot usa o título da loja ou o nome que vem no próprio endereço (útil no TPT, que bloqueia leitura).
- **Observações** vão para o Claude ao escrever as copies: público, diferenciais, época do ano... Pode escrever em português; as copies saem em inglês US ou UK, conforme a loja.
- **Mockup:** um caminho (`data/mockups/x.png`) ou uma URL `.png`/`.jpg` na linha vira a imagem do Pin. Sem isso, o bot usa a capa da loja quando consegue (TES, Notion e Etsy via API).
- **Produto pendente:** se um agente já tinha criado o produto, o link é ligado a ele automaticamente.
- **Sem duplicatas:** não precisa apagar as linhas antigas, porque produtos já cadastrados não são repetidos.
- **Quando é lido:** o PC lê o arquivo a cada 4 h (refill) e ao abrir o `adicionar_links.bat`.

### 🔎 Sincronização automática com as suas lojas (`data/stores.json`)
Os links das suas áreas ficam em `data/stores.json`. O refill do PC (a cada 4 h) e o `adicionar_links.bat` varrem essas lojas e:
- **ligam sozinhos** os produtos pendentes cujo título bate com um anúncio da loja (título praticamente igual e sem outro anúncio parecido disputando);
- **sugerem** o anúncio quando o título só é parecido (no `adicionar_links.bat`, basta dar Enter para aceitar);
- **importam** os anúncios que estão na loja mas não no bot, com a capa do anúncio como imagem. Assim o catálogo antigo também é divulgado. Para desligar, use `"import_unknown": false`.

| Loja | Como é lida | Situação |
|---|---|---|
| TES | Busca por autor ("View all" da loja): catálogo completo, com títulos, links e capas | ✅ Automático |
| Notion | Perfil de criador (nome, link, descrição e imagem) | ✅ Automático |
| Etsy | **API oficial** (a página da loja bloqueia programas) | ⚙️ Precisa de `ETSY_API_KEY` |
| TPT | Bloqueia programas e não tem API pública | ✋ Manual pelo `adicionar_links.bat` |

**Chave do Etsy (gratuita):**
1. Acesse https://www.etsy.com/developers/your-apps e crie um app ("Create a new app").
2. Copie a **Keystring** e o **Shared secret**.
3. Coloque no `.env` do PC: `ETSY_API_KEY=<keystring>` e `ETSY_SHARED_SECRET=<shared secret>`.

Se o Etsy deixar o app como "pending", a sincronização do Etsy começa a funcionar quando ele for aprovado. Até lá, os links do Etsy entram pelo `adicionar_links.bat`.

### Formato completo
Produtos já publicados podem ir direto com o `url`, como no exemplo abaixo:

```json
{
  "products": [
    {
      "id": "tpt-fractions-escape-room",
      "title": "Fractions Escape Room - 4th Grade Math Review",
      "description": "No-prep printable and digital escape room...",
      "url": "https://www.teacherspayteachers.com/Product/...",
      "platform": "tpt",
      "image_url": "https://.../mockup.jpg",
      "created_at": "2026-10-20T10:00:00Z",
      "tags": ["fractions activity", "4th grade math"],
      "price": "$4.50",
      "stats": {"views": 1840, "sales": 37}
    }
  ]
}
```

- `platform` aceita: `tpt`, `teacherspayteachers`, `tes`, `tes.com`, `notion`, `notion marketplace`, `etsy`. A plataforma define o idioma: **TES → en-GB**, as demais → **en-US**.
- Os campos são flexíveis (`name`/`title`, `link`/`url`, `mockup_url`/`image_url`...). O `id` é gerado automaticamente se faltar, mas é melhor fixá-lo para o histórico não se perder.
- `"active": false` tira um produto da rotação.
- `board_id` no produto sobrescreve o mapa de boards.

### Priorização por desempenho
Atualize `stats` no próprio produto ou em `data/stats.json` (`{"<id>": {"views": 120, "sales": 4, "clicks": 30}}`).
O peso de cada produto combina **vendas** (peso 3), **cliques/favoritos** (1,5) e **visualizações** (1), em escala logarítmica. Os pesos são ajustados assim:
- **Produto nunca publicado**: 3×.
- **Lançamento recente**: 1,8×.
- **Fadiga**: o peso cai conforme o produto acumula Pins, mas cai menos para quem vende bem.
- **Tempo sem aparecer**: até 3×.

Os produtos são sorteados com esses pesos, então a ordem é sempre aleatória. Na simulação com 20 produtos, os mais vendidos receberam cerca de 2× mais Pins, e nenhum produto apareceu duas vezes seguidas.

### Variações e ciclos
Cada produto tem de 3 a 5 variações (template + paleta + badge + CTA + copy com ângulo diferente: dor→solução, benefício, keyword, lista, sazonal). Quando todas são publicadas, abre-se um **novo ciclo**, com copies novas do Claude e um novo sorteio de templates e paletas. Assim o Pinterest sempre recebe *fresh pins*, que o algoritmo favorece.

---

## 💻 Rodando localmente

```bash
pip install -r requirements.txt
python scripts/download_fonts.py
cp .env.example .env   # preencha
python -m src.main --dry-run --no-jitter --count 3     # gera sem publicar
python -m src.main --preview tpt-fractions-escape-room # renderiza todas as variações de um produto
```
As imagens ficam em `output/pins/`.

## ⏰ Horários (UTC)
`12:17`, `17:43`, `01:11` e `03:37`: manhã dos EUA/almoço do Reino Unido, pico noturno do Reino Unido, horário nobre da costa leste e noite da costa oeste. Os minutos "quebrados" evitam a fila do GitHub no minuto :00. O cron do GitHub pode atrasar de 5 a 20 minutos, o que também ajuda a parecer orgânico.

## 🛠 Solução de problemas
| Sintoma | Causa provável |
|---|---|
| `Canal 'webhook' desativado... (410/404)` | Cenário do Make desligado ou URL trocada |
| `Webhook exige URL pública da imagem` | Repositório privado com `IMAGE_HOST=github`: use `imgbb` |
| `Token do Pinterest expirado e não renovável` | Refresh token expirado: gere outro e atualize o Secret |
| `403` na API | App em Trial ou sem o escopo `pins:write` |
| `Fila de copies vazia... usando template` | O PC não fez refill. Veja `logs/refill.log` ou rode `python scripts/local_refill.py --status` |
| Refill: `Claude Code não encontrado` | Defina `CLAUDE_BIN` no `.env` com o caminho do `claude.exe` |
| Refill: `Limite de uso da assinatura atingido` | Normal: ele continua sozinho na próxima janela |
| Refill gera mas não chega ao GitHub | Credenciais do Git: faça um `git push` manual uma vez no PC |
| Push do histórico falhou | Ative *Read and write permissions* (passo 6) |
