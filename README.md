# VisionEPI

Revisão do piloto com cinco câmeras, correções e resultados de validação:
[Revisão da branch com rebrand](docs/REVISAO-REBRAND.md).

Novos recursos para o NEXT — trajetórias, zonas, exposição postural, missões e
laboratório: [uso e limites da prevenção](docs/PREVENCAO-NEXT.md).

Gamificação: [níveis, conquistas e metas da jornada de prevenção](docs/GAMIFICACAO.md).

Sistema de monitoramento de segurança industrial em tempo real, desenvolvido para o **Challenge 2026 — Metaindústria** (FIAP × SPI Integração). Usa visão computacional para detectar uso de EPIs, analisar postura e comportamento, e sinalizar risco antes que o incidente aconteça — não apenas registrar a infração depois do fato.

## O problema

O modelo tradicional de segurança industrial é reativo: inspeções periódicas, checklists manuais, punição depois do incidente. O VisionEPI propõe o oposto — monitoramento contínuo por IA, alertas em tempo real, e uma camada de tendência que aponta onde o risco está se acumulando antes de virar acidente.

## Funcionalidades

- **Detecção de EPIs em tempo real** — capacete, colete, luvas, óculos, máscara e calçado de segurança, via modelo YOLOv8 dedicado (Vyra, 14 classes). O segundo modelo para detecção de pessoa continua disponível para pesos que não trazem a classe `Person` (arquitetura dual-model opcional).
- **Multi-pessoa com identidade estável** — múltiplas pessoas detectadas e avaliadas no mesmo frame, cada uma com id que persiste entre frames (tracking por IoU). O EPI é associado à pessoa por geometria e de forma exclusiva: um capacete pertence a uma pessoa só, mesmo com as caixas se sobrepondo.
- **Multi-câmera de verdade** — um worker por câmera, modelos YOLO carregados uma única vez e compartilhados. Todo alerta, evento e mensagem de WebSocket carrega `camera_id`.
- **Análise de postura e quedas** — via MediaPipe Pose, sinalizando posturas suspeitas e pessoas caídas.
- **Área de risco configurável** — editor visual de zona de risco; alerta quando uma pessoa entra na área.
- **Ciclo de vida de alertas com histerese** — alertas são criados/resolvidos após N frames consecutivos (não a cada frame instável), evitando ruído de falso positivo.
- **Tendência de risco** — score agregado por categoria (os seis EPIs + quedas/postura/área de risco), opcionalmente filtrado por câmera, calculado sobre o histórico real de alertas em janela deslizante, com sparkline de 24h. Estatística honesta sobre o histórico — não é predição de IA, é isso que os dados sustentam hoje.
- **Command palette (Ctrl/Cmd+K)** — navegação rápida entre painéis, troca de modo, iniciar/parar monitoramento, sem precisar do mouse.
- **Alertas sonoros** — som curto para alertas críticos e para o retorno a "tudo certo", com mute persistente e sempre visível.
- **Autenticação real com papéis** — login por e-mail e senha (hash scrypt), sessão em cookie HttpOnly, e três papéis hierárquicos que decidem o que cada pessoa pode fazer. Sem cadastro público: quem cria acesso é quem já tem acesso.
- **Modo Operador / Técnico / Supervisor** — visão essencial para o campo (com ações reais de "avisei o colaborador" e "marcar falso positivo"), e um painel de diagnóstico completo (FPS, classes do modelo, diagnósticos de detecção) para quem precisa investigar.
- **Dashboard em tempo real** via WebSocket (Socket.IO) — feed de vídeo, conformidade por pessoa, linha do tempo de eventos, tudo atualizado ao vivo.

## Stack

**Backend**
- Python 3.11–3.12 · Flask · Flask-SQLAlchemy · Flask-Migrate (Alembic) · Flask-SocketIO
- gunicorn em produção (o servidor de desenvolvimento do Werkzeug nunca é usado fora de dev)
- Ultralytics YOLOv8 (detecção de EPI e de pessoa, dual-model)
- MediaPipe Pose (postura e quedas)
- SQLite (dev) / PostgreSQL (produção, via Docker)

**Frontend**
- React 18 + TypeScript + Vite
- Zustand (estado global, assinando os eventos WebSocket)
- Socket.IO client
- `motion` (animação declarativa), `@number-flow/react` (transição de números), `cmdk` (command palette)
- CSS global com design tokens próprios. Direção visual calma: superfície neutra clara (escuro suportado), hierarquia por espaço e tipografia Geist, cor guardada para o estado que importa (laranja de perigo pela ABNT NBR 7195, vermelho só no botão Parar, verde só em ponto e rótulo), Geist Mono apenas em número que alinha em coluna

## Arquitetura

```mermaid
flowchart TB
    subgraph thread["Thread de captura — uma por câmera"]
        direction TB
        FONTE["<b>VideoStream</b><br/>RTSP · USB · arquivo<br/>retry com backoff e teto"]
        LOOP["<b>_loop</b> — um frame por vez"]
        YOLO["YOLO EPI + YOLO pessoa<br/><i>inference_lock compartilhado</i>"]
        POSE["MediaPipe Pose<br/>por pessoa rastreada"]
        REGRAS["RuleEngine + PersonComplianceMatcher"]
        ALERTA["AlertStateService<br/><i>histerese conta DETECÇÃO</i>"]
        ENCODE["cv2.imencode<br/><i>uma vez por frame, 2,5 ms</i>"]
        FONTE --> LOOP --> YOLO --> POSE --> REGRAS --> ALERTA --> ENCODE
    end

    FIXTURE["<b>Modo fixture</b><br/>bench.mp4 em loop<br/><i>após N tentativas</i>"]
    FONTE -. "fonte morta" .-> FIXTURE
    FIXTURE -. "assume a fonte" .-> LOOP

    subgraph llm["Camada LLM — FORA da thread do frame"]
        direction TB
        SUBMETER["<b>submeter()</b><br/><i>9–13 µs · nunca bloqueia</i>"]
        PORTAO{"debounce 15 s<br/>+ uma em voo<br/>por câmera?"}
        DESCARTE["descarta<br/><i>78 de 79 eventos</i>"]
        EXECUTOR["thread daemon<br/>ProvedorGemini"]
        SCHEMA{"cabe no<br/>schema?"}
        NADA["None + log<br/><i>nunca vira alerta</i>"]
        OPINIAO["<b>segunda_opiniao</b><br/><i>campo separado</i>"]
        SUBMETER --> PORTAO
        PORTAO -- "não" --> DESCARTE
        PORTAO -- "sim" --> EXECUTOR --> SCHEMA
        SCHEMA -- "não" --> NADA
        SCHEMA -- "sim" --> OPINIAO
    end

    ALERTA == "alerta CRIADO<br/>+ o JPEG já codificado" ==> SUBMETER

    BANCO[("SQLite / PostgreSQL<br/>alertas · eventos · câmeras · usuários")]
    FLASK["<b>Flask</b><br/>API REST + Socket.IO<br/><i>escopo por câmera em rooms</i>"]
    SPA["<b>React SPA</b> (Vite)<br/>vídeo MJPEG + telemetria"]

    ALERTA --> BANCO
    ENCODE --> FLASK
    OPINIAO --> FLASK
    BANCO --> FLASK
    FLASK <--> SPA

    classDef sync fill:#1e3a5f,stroke:#3b82f6,color:#e0f2fe
    classDef async fill:#3f2d1e,stroke:#f59e0b,color:#fef3c7
    classDef store fill:#1f3a2e,stroke:#22c55e,color:#dcfce7
    classDef fall fill:#3f2d1e,stroke:#f59e0b,color:#fef3c7,stroke-dasharray: 5 3
    class FONTE,LOOP,YOLO,POSE,REGRAS,ALERTA,ENCODE sync
    class SUBMETER,PORTAO,DESCARTE,EXECUTOR,SCHEMA,NADA,OPINIAO async
    class BANCO,FLASK,SPA store
    class FIXTURE fall
```

**O que o diagrama diz, e por que importa:**

- **Azul é o caminho do frame**, e ele é síncrono. Todo o orçamento de tempo
  vive ali: 19,56 fps com uma câmera a `imgsz=416`
  ([BENCH.md](docs/BENCH.md)). Um `inference_lock` compartilhado serializa a
  inferência entre câmeras — é por isso que multi-câmera **não escala**, e a
  espera foi medida em 134 a 237 ms com 2 câmeras.
- **Âmbar é assíncrono e descartável.** A camada LLM é acionada só quando um
  alerta é **criado**, reaproveita o JPEG que o loop já codificou (não há
  segundo `imencode`) e devolve o controle em 9–13 µs. Quando já há uma chamada
  em voo ou o debounce está fechado, o evento é **descartado, não enfileirado**:
  enfileirar produziria respostas descrevendo uma cena que já passou.
- **A seta do LLM não volta para os alertas.** Ela vai para `segunda_opiniao`,
  campo separado. O modelo de linguagem **não** cria, resolve nem suprime
  alerta — informa o operador, não decide. Resposta que não cabe no schema vira
  `None` e log.
- **Modo fixture é estado de primeira classe** (tracejado), não falha
  silenciosa: depois de `RTSP_MAX_TENTATIVAS` tentativas o worker assume a
  fixture em loop e a UI mostra "modo fixture — fonte de demonstração". Ver
  [DEMO.md](docs/DEMO.md).

Em desenvolvimento, o Vite roda como servidor separado (`:5173`) com proxy para o Flask (`:5000`). Em produção, o Flask serve o build estático do React diretamente.

## Como rodar

### Pré-requisitos
- **Python 3.11 ou 3.12** — não 3.13+. `mediapipe==0.10.14` e `numpy==1.26.4` não publicam wheel para versões acima da 3.12, e `pip install` falha antes de instalar qualquer coisa.
- Node.js 20.19+ ou 22.12+ e npm (requisito do build com Vite 7; [migração oficial](https://v7.vite.dev/guide/migration#node-js-support))
- (Opcional) Docker, se for usar PostgreSQL em vez de SQLite

### Backend

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
copy .env.example .env          # Windows
# cp .env.example .env          # macOS/Linux
```

Edite o `.env`: gere a `SECRET_KEY` (a aplicação **não sobe** sem ela); mantenha `DATABASE_URL` comentada para usar SQLite local, ou suba um Postgres via `docker compose up -d postgres` e descomente a URL.

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Crie o esquema do banco:

```bash
flask --app wsgi db upgrade
```

Crie a primeira conta — sem ela não há como entrar:

```bash
flask --app wsgi users create --role supervisor
```

> **`AUTO_CREATE_TABLES` precisa estar `false`** (é o padrão do `.env.example`). Quem é dono do esquema é o Alembic. Com `true`, `create_app()` chama `db.create_all()` — e o CLI do Flask constrói a aplicação **antes** de executar o subcomando, então as tabelas nascem sem `alembic_version` e o `db upgrade` seguinte morre com `table alerts already exists`, **num clone limpo**. Se você ligou `AUTO_CREATE_TABLES=true` e caiu nesse erro, apague o banco (`instance/visionepi-dev.db`) e rode o `upgrade` de novo com a variável em `false`.
>
> Caso diferente: **já tinha um banco da versão anterior**, criado por `db.create_all()` quando ainda não havia migrações? Aí sim rode `flask --app wsgi db stamp f6cd160ae4e0` **uma vez** antes do `upgrade`, para o Alembic aplicar só as migrações novas em vez de recriar tabelas existentes. Isso **não** resolve o caso do clone limpo acima — são problemas distintos.

O caminho documentado acima é coberto por `tests/test_onboarding.py`.

### Frontend

```bash
cd frontend
npm install
npm run build
```

### Rodando tudo

```bash
python run.py
```

Acesse `http://localhost:5000`. A porta vem de `PORT` no `.env` e é a mesma usada por `run.py`, Dockerfile, docker-compose e pelo proxy do Vite.

`python run.py` é o servidor de **desenvolvimento**. Em produção quem serve é o gunicorn sobre `wsgi:app` — é o que o `Dockerfile` faz. `FLASK_DEBUG` nunca deve ser `true` fora da sua máquina: o modo debug do Werkzeug expõe um console interativo que executa código arbitrário.

Para desenvolvimento do frontend com hot-reload, rode `npm run dev` dentro de `frontend/` em paralelo ao `python run.py` — o Vite abre em `http://localhost:5173` e faz proxy das chamadas de API/WebSocket para o Flask.

### Configuração do modelo de EPI

O `.env` aponta `PPE_MODEL_PATH` para o modelo de detecção de EPI. O padrão é **`models/vyra_ppe.pt`** ([Hexmon/vyra-yolo-ppe-detection](https://huggingface.co/Hexmon/vyra-yolo-ppe-detection), YOLOv8m, 14 classes, licença **CC-BY-4.0 — exige atribuição ao autor**).

Os pesos **não são versionados** (`.gitignore: *.pt`): baixe o arquivo e salve em `models/vyra_ppe.pt`.

Esse modelo tem a classe `Person` no índice 11 — mas veja a ressalva abaixo antes de confiar nela.

#### A classe `Person` do Vyra não generaliza — use `MULTI_PERSON_DETECTION=true`

O modelo **tem** a classe `Person`, e por isso o projeto nasceu com `MULTI_PERSON_DETECTION=false`. Medindo, a premissa não se sustenta:

- **36 células testadas** (3 fotos independentes de canteiro de obra com pessoas de corpo inteiro × `imgsz` ∈ {416, 640, 960, 1280} × `conf` ∈ {0,35; 0,10; 0,02}, instância nova do modelo a cada célula): **zero detecções de `Person`**. Nas mesmas imagens o modelo detecta `Hardhat` e `Safety Vest` normalmente.
- Somando ~650 quadros amostrados de quatro vídeos de segurança do trabalho (CDC/NIOSH), também **zero** `Person`.
- A causa está na **matriz de confusão publicada pelo próprio autor** ([confusion_matrix.png](https://huggingface.co/Hexmon/vyra-yolo-ppe-detection/blob/main/confusion_matrix.png)): `Person` tem ~**277 instâncias** de validação contra ~**8.946** de `Hardhat`. É a menor classe real do dataset — ~32x menos suportada. Ela funciona na distribuição de treino dela e falha fora.
- Para comparação, na mesma imagem `yolov8n.pt` (COCO) detecta as duas pessoas com confiança **0,87** e **0,73**.

**Consequência:** com `MULTI_PERSON_DETECTION=false`, `person_compliance_matcher.py:83` recebe lista de pessoas vazia. O sistema desenha capacetes e coletes no vídeo e **nunca avalia a conformidade de ninguém** — nenhum alerta de EPI é criado.

Por isso **`MULTI_PERSON_DETECTION=true` é o padrão** do `.env.example`, travado por `tests/test_onboarding.py`. O custo é declarado, não escondido: **−20% de FPS** (24,32 → 19,42 a `imgsz=416`, ver [docs/BENCH.md](docs/BENCH.md)). É o preço de o sistema fazer o que promete.

Isso exige o segundo peso, também não versionado. Baixe uma vez e mova para `models/`:

```bash
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
```

`PERSON_MODEL_PATH` aponta para `models/yolov8n.pt` de propósito: com o nome solto (`yolov8n.pt`), o ultralytics baixa o peso no diretório de trabalho de quem rodou — fora de `models/`, que é o único lugar ignorado pelo git.

#### `YOLO_IMGSZ=416` está abaixo da resolução de treino

O `args.yaml` publicado com os pesos registra `imgsz: 640`. O projeto infere a **416** por custo — a 640 o YOLO custa **166 ms** contra **82 ms** (medido, [docs/BENCH.md](docs/BENCH.md)). O preço da economia é detecção: na foto de teste, a 416 o modelo encontra **1** caixa e a 640 encontra **2**, e o capacete só aparece a partir de 640.

#### Classes detectadas que nenhuma regra consome

`Fall-Detected` (0) e `Safety Cone` (12) estão em `YOLO_CLASSES`, são detectadas, **desenhadas no vídeo** (`annotator.py:77,90`) e vão no payload do WebSocket — mas **nenhuma regra as lê**. Quem decide "pessoa caiu" é exclusivamente a geometria de landmarks do MediaPipe (`risk_rules.py:230-243`); a detecção de queda do YOLO é sinal não consumido.

Removê-las de `YOLO_CLASSES` **não economizaria nada**: o filtro `classes=` do ultralytics é aplicado **depois** do NMS (`ultralytics/utils/ops.py:273`), então o forward pass calcula as 14 classes de qualquer jeito. Por isso elas continuam ali.

#### Alternativa

Ligue `MULTI_PERSON_DETECTION=true` também ao usar um modelo de EPI sem classe `person` (ex.: `models/epi_pretrained.pt`) — e, nesse caso, **limpe `YOLO_CLASSES`**, porque os índices em `.env.example` são específicos do Vyra.

Sem um modelo de EPI treinado/compatível configurado, o dashboard mostra aviso de "modelo não suportado" — o restante do sistema (detecção de pessoa, pose, área de risco) funciona normalmente mesmo assim.

#### OpenVINO e ONNX: avaliados e não adotados

O código aceita `PPE_MODEL_PATH` apontando para um diretório `*_openvino_model`, mas o `requirements.txt` **não instala OpenVINO** — ele foi avaliado e rejeitado (commit `2d74e96`). O ONNX foi medido nesta máquina com o `best.onnx` que o próprio Hexmon publica: **33% mais lento** que o PyTorch, não os "up to 3x" que a documentação da Ultralytics anuncia. Números em [docs/BENCH.md](docs/BENCH.md). `onnxruntime` **não** está no `requirements.txt`.

## Autenticação e papéis

Todas as rotas da API exigem sessão. Os três papéis são **hierárquicos** —
supervisor pode tudo que o técnico pode, e assim por diante:

| | Operador | Técnico | Supervisor |
|---|---|---|---|
| Ver vídeo, alertas, conformidade, linha do tempo | ✅ | ✅ | ✅ |
| Iniciar/parar monitoramento | ✅ | ✅ | ✅ |
| Marcar falso positivo / avisar colaborador | ✅ | ✅ | ✅ |
| Cadastrar e configurar câmeras | — | ✅ | ✅ |
| Alterar configurações, overlay e área de risco | — | ✅ | ✅ |
| Gerir usuários | — | — | ✅ |

O Operador é restrito à **câmera do setor dele** (`User.camera_id`, definido pelo supervisor): lista de câmeras, vídeo, alertas, evidências, linha do tempo e score de risco vêm só daquela área, e ele não consegue iniciar nem parar o monitoramento de outra. Uma conta de operador **sem setor atribuído não vê nada** até o supervisor definir — deixar passar daria acesso amplo justamente à conta incompleta. Técnico e Supervisor veem o parque inteiro, porque é o trabalho deles.

Comandos de gestão:

```bash
flask --app wsgi users create --role operator --camera-id 1
```

```bash
flask --app wsgi users list
```

```bash
flask --app wsgi users set-password --email pessoa@empresa.com
```

Detalhes que importam para a avaliação de segurança:

- Senha nunca é persistida nem registrada em log — só o hash **scrypt** do werkzeug.
- Sessão em cookie **HttpOnly** (JavaScript da página não lê, então XSS não rouba a sessão) e **SameSite=Lax**. Atrás de HTTPS, ligue `SESSION_COOKIE_SECURE=true`.
- A aplicação **não sobe** com uma `SECRET_KEY` que conste no repositório (`change-me` do `.env.example`, o default do `config.py`, e afins) nem com uma chave curta demais. Esse segredo assina a sessão: com um valor público, qualquer pessoa forja o cookie de um supervisor sem credencial.
- E-mail inexistente e senha errada devolvem a **mesma** resposta, e o custo de verificação é constante — não dá para descobrir quais contas existem.
- Cinco tentativas erradas travam a conta por um tempo que dobra a cada rodada (até 30 min).
- Desativar uma pessoa, ou trocar a senha dela, **revoga todas as sessões** já emitidas — inclusive um cookie que tivesse sido copiado. Cada sessão carrega a `session_epoch` vigente no login, e o servidor compara a cada request.
- Sair encerra a sessão **naquele navegador**, não em todos: derrubar tudo a cada logout expulsaria a pessoa do kiosk do chão de fábrica quando ela saísse do desktop. Para revogar em todos os lugares (conta comprometida), troque a senha.
- O socket revalida a sessão a cada 30 s. Sem isso, uma conexão já aberta seguiria recebendo vídeo e alertas depois de o acesso ser revogado.
- O **WebSocket** também exige sessão — proteger só o REST deixaria o feed de análise e alertas acessível pela porta dos fundos.
- O **escopo de câmera vale no WebSocket também**, via rooms do Socket.IO (`app/utils/salas.py`). Técnico e Supervisor entram na sala `parque`; o Operador entra só em `camera:<id>` do setor dele; Operador sem setor não entra em sala nenhuma. Antes disso, todo `emit` era broadcast: o operador que recebia 404 ao pedir outra área por HTTP recebia `analysis`, `compliance_state` e `active_alerts` daquela área pelo socket, ~12 vezes por segundo. Coberto por `tests/test_escopo_socket.py`.

## Testes

Roteiro de teste **manual** (do zero até os casos de borda), em [`docs/TESTE-MANUAL.md`](docs/TESTE-MANUAL.md).

Verificação automatizada:

```bash
pytest
```

Lint:

```bash
ruff check .
```

Typecheck do frontend:

```bash
npm --prefix frontend run build
```

## Estrutura do projeto

```
app/                    Backend Flask
  api/                  Blueprints REST (status, alerts, cameras, monitor, risk, ...)
  services/             Lógica de negócio (monitor, workers, alertas, compliance, risk score)
  repositories/         Acesso a dados
  vision/               Detecção YOLO, tracking, pose, matching pessoa-EPI
  models.py             Modelos SQLAlchemy
migrations/             Migrações Alembic (Flask-Migrate)
frontend/               SPA React + TypeScript + Vite
  src/
    api/                Cliente REST, tipos e chaves de EPI
    socket/             Cliente WebSocket
    store/              Estado global (Zustand)
    components/         Componentes por domínio (vídeo, alertas, features, etc.)
tests/                  Testes backend (pytest)
```

## Roadmap

- [x] Tracking estável de pessoa entre frames — feito com um tracker IoU por câmera (`app/vision/person_tracker.py`) em vez de `model.track(persist=True)`: o estado do tracker do Ultralytics vive dentro do objeto do modelo, e os modelos aqui são compartilhados entre câmeras.
- [x] Matching geométrico EPI–pessoa por posição real, com atribuição exclusiva
- [x] Autenticação nos endpoints REST sensíveis — login com papéis, cobrindo REST e WebSocket
- [x] Retry/backoff no stream de vídeo, com teto — e fallback para fonte de demonstração quando o teto não resolve. Exercitado contra um **servidor RTSP local com usuário e senha**, não só contra dublê: mata o servidor → backoff dispara e o estado aparece; religa → reconecta sozinho (`total_reconnects=1`); não religa → assume a fixture e segue publicando frame. 12 testes em `tests/test_camera_worker_falha_de_fonte.py` + 7 em `tests/test_video_stream.py`, e o log do ciclo completo em [DEMO.md](docs/DEMO.md).
- [x] Segunda opinião multimodal ligada no boot — disparo no alerta criado, fora da thread do frame (`submeter()` em 9–13 µs), com re-bench dentro do ruído. O LLM não cria, resolve nem suprime alerta. 11 testes em `tests/test_camera_worker_llm.py`; `LLM_ENABLED=false` é o default e o sistema funciona sem chave.
- [ ] **Pose por pessoa com instância própria** — os recortes por pessoa já existem (`POSE_PER_PERSON=true`, `tests/test_pose_por_pessoa.py`), mas todas as pessoas passam pela **mesma** instância do MediaPipe em `static_image_mode=True`. Trocar para o modo de stream mediu **2,25x** (34,10 → 15,14 ms de p50), e não foi aplicado: a doc oficial descreve `True` como o modo para "a batch of static, possibly unrelated, images", que é exatamente o que `estimate_for_people` alimenta. O caminho que dá os dois é uma instância por track rastreado. Número e citação em [BENCH.md](docs/BENCH.md).
- [ ] Feature por câmera em runtime — `PUT /api/cameras/<id>` grava no banco, mas o worker em execução só relê a configuração quando é reconstruído (mudança de fonte/fps/resolução)
- [ ] Multi-câmera que escala — a contenção está medida e é o `inference_lock` cobrindo YOLO + pose de todas as câmeras (134 a 237 ms de espera pura com 2 câmeras). Dobrar câmeras hoje não aumenta o throughput agregado em nada: cada uma recebe metade.

## Equipe

Desenvolvido para o Challenge 2026 (FIAP × SPI Integração), com mentoria de Fernando V. Marcolina e Wendel de Almeida Passos.
