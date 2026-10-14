# Revisão e correções para o piloto com cinco câmeras

## Base e objetivo

Esta revisão usa `origin/feat/completo-com-rebrand`, commit
`e3a8af7dbfa4f5b4b1a92518973bdb8526aae14c`. As alterações estão na branch local
`codex/rebrand-improvements`, no diretório `pilot-rebrand`.
A análise anterior de `main` não deve ser usada como diagnóstico desta versão.
As alterações daquela análise foram preservadas em `project-review`.

O objetivo informado é operar cinco câmeras IP simultaneamente na planta,
mantendo os recursos do projeto e buscando a menor latência possível.
Esta etapa corrige problemas de isolamento, estado, interface, configuração e
verificação automatizada. Ainda não é uma medição de desempenho com as câmeras
da planta: os endereços RTSP e os pesos usados pelos colegas não estavam
disponíveis nesta execução.

## O que esta branch já tinha

O rebrand não é apenas uma troca visual. A base já contém trabalhadores por
câmera, regras de área de risco, rotação da imagem, modo portaria, associação de
EPI por pessoa, histerese de alertas, configurações de modelos e segunda opinião
assíncrona. Esses recursos foram mantidos.

O README e a configuração descrevem o modelo Vyra com máscara entre os EPIs.
Portanto, a conclusão anterior sobre ausência de máscara na configuração de
`main` não se aplica automaticamente a esta branch. A cobertura real depende
das classes dos pesos carregados, do mapeamento e do resultado nas imagens.
A validação dos pesos efetivamente usados na apresentação continua necessária.

Também já havia preservação do reconhecimento de alertas durante atualizações
e configuração de zona por câmera. A revisão trabalhou sobre essas correções,
sem substituir os arquivos pela implementação de `main`.

## Problemas corrigidos

| Problema encontrado | Consequência | Correção aplicada |
| --- | --- | --- |
| Evidências acessadas por rotas com regras diferentes | Possível exposição de imagens de outra câmera ou consulta dependente de worker ativo | Serviço único de evidências, autorização pela câmera do alerta, validação de referência, caminho canônico e cache privado sem armazenamento |
| Operador sem câmera em algumas consultas | Consulta sem o isolamento esperado | Resposta de acesso negado em risco e configurações; operador consulta sua própria câmera por padrão |
| Alterar usuário não encerrava necessariamente conexões já abertas | Socket ou vídeo podia continuar depois da mudança de acesso | Desconexão dos sockets do usuário e invalidação dos streams; consulta periódica também detecta mudanças externas ao processo |
| Marcar falso positivo atualizava o banco, mas podia deixar estado antigo no worker | Alerta reaparecia ou continuava na lista ativa | Propagação de snapshots para o worker, sobreposição imediata no estado consultável e atualização do registro antes de novos toques |
| Evento de lista ativa repetido com conteúdo idêntico | Trabalho e tráfego desnecessários no navegador | Comparação com cópia do último estado emitido; alterações reais continuam emitindo eventos |
| Eventos de outra câmera acionavam efeitos de interface | Som, destaque ou resolução incorretos na câmera selecionada | Filtragem de criação e resolução antes dos efeitos do frontend |
| Recursos da câmera padrão compartilhados de forma diferente das demais | Configuração podia vazar entre câmeras ou perder persistência | Gerenciador individual por worker e sincronização da configuração persistida |
| Mudança de recurso podia manter análise em cache | Resultado anterior continuava aparecendo após a alteração | Invalidação do cache ao aplicar configuração; atualização do worker existente |
| FPS alterado em execução afetava configuração global | Uma câmera interferia nas demais | FPS por worker, intervalo recalculado no loop e distinção entre valor persistido e temporário |
| Tracking inconsistente entre modelo único e duplo | Identidades instáveis ou caixas de pessoa duplicadas | Tracking por câmera em ambos os caminhos e remoção da duplicação no modelo duplo |
| Diagnóstico ignorava suporte a pessoa do modelo principal | Informação incorreta sobre capacidade do detector | Suporte inicial derivado do diagnóstico do modelo principal |
| Teste geométrico com denominador inadequado | Zona podia mudar de resultado conforme direção das arestas | Função única para polígonos, incluindo bordas, concavidade e ambas as orientações |
| Ausência de alerta era interpretada como conformidade | Tela podia ficar verde sem verificação válida | Conformidade depende do resultado atual; estados desconhecido e não suportado são explícitos |
| Imagem parada ou fallback podia parecer monitoramento normal | Operador podia confiar em imagem desatualizada | Idade do último frame, diagnóstico de imagem desatualizada e mensagens para fonte parada, indisponível ou em fixture |
| Teste de falha dependia de MP4 ausente e não tinha limite de espera | Suíte podia travar indefinidamente | Vídeo local gerado no teste e limite de tentativas com mensagem de erro |
| Porta do Compose divergente do `PORT` do servidor | Publicação incorreta ao configurar outra porta | Mapeamento usa a mesma variável em ambos os lados |
| SQLite sem volume próprio | Dados podiam desaparecer ao recriar o container | Volume persistente em `/app/instance` |
| Poucas threads HTTP diante de feeds longos | Cinco MJPEG e polling podiam disputar capacidade com a API | Padrão configurável de 32 threads, mantendo um processo Gunicorn |
| Dependências de desenvolvimento do frontend com vulnerabilidades reportadas | Ferramentas desatualizadas | Atualização do Vite e plugin React, lockfile atualizado e requisito de Node documentado |

### Alertas e concorrência

A sincronização entre API e worker usa cópias dos payloads e proteção do estado
compartilhado. Não transporta objetos ORM entre threads. Isso reduz divergências
entre o que o usuário acabou de fazer, o banco e a próxima análise.
Os testes cobrem o fluxo de falso positivo e reconhecimento; não constituem
uma prova de ausência de todas as corridas sob qualquer carga.

### Interface do operador

O estado saudável sem alertas passa a informar “Sem alertas ativos”. Um EPI só
fica conforme quando existe resultado atual que o confirma. Falta de suporte
do modelo, resultado desconhecido e falha de monitoramento têm apresentação
própria. Assim, desconexão e ausência de avaliação não são confundidas com
segurança confirmada.

### Isolamento e acesso

Evidências são autorizadas antes de servir o arquivo. Arquivos sem alerta
associado não ficam acessíveis pela rota de diagnóstico. Referências não podem
introduzir subdiretórios ou atravessar o diretório de snapshots.

No processo atual, alterações de acesso pela API invalidam o stream na próxima
iteração e desconectam os sockets. Mudanças feitas fora dele são verificadas no
máximo uma vez por segundo, evitando uma consulta ao banco por frame.

## Desempenho: o que mudou e o que continua aberto

Foram retirados eventos redundantes e corrigidas disputas desnecessárias de
configuração e capacidade HTTP. Nenhum resultado desta etapa demonstra FPS ou
latência reais das cinco câmeras. Também não foi reduzida a cobertura de EPIs
nem desligado recurso para produzir um número artificial de desempenho.

O caminho de captura, inferência, pose, regras e codificação continua síncrono
dentro de cada worker. Os modelos compartilhados continuam protegidos pelo
lock de inferência. Portanto, cinco workers não significam cinco inferências
simultâneas na GPU. Uma câmera lenta ou modelos caros ainda podem aumentar o
tempo total de análise, mesmo em um computador potente.

O parâmetro `DETECTION_EVERY_N_FRAMES` e a histerese também participam do tempo
até o alerta. FPS de vídeo, frequência de detecção e latência de alerta são
medidas diferentes. Aumentar FPS configurado sem medir a idade do frame pode
apenas aumentar a fila na fonte, conforme o comportamento do backend RTSP.

As próximas decisões de arquitetura devem partir de um teste com cinco fontes:

1. Medir cada câmera sozinha e depois as cinco juntas, com todos os recursos
   necessários ligados e a mesma resolução/modelos.
2. Registrar FPS de captura e de análise, idade do frame, tempos por estágio,
   uso de CPU/GPU, memória e reconexões.
3. Medir a latência entre uma infração encenada e o alerta visível; informar
   mediana e percentis, além de médias.
4. Desconectar uma fonte durante o teste, reconectar e confirmar que as outras
   quatro continuam operando e que a interface indica a falha corretamente.
5. Só então decidir sobre captura desacoplada com buffer do último frame,
   agendamento de inferência/batching e redução de custos de pose/codificação.

A proposta de captura desacoplada busca analisar imagem recente em vez de
acumular atraso. Batching pode melhorar throughput, mas também acrescentar
espera: não deve ser adotado como melhoria garantida de latência sem medição.

## Verificações executadas

| Verificação | Resultado |
| --- | --- |
| Suíte completa Python, sem exclusões | **429 testes passaram**, 3 avisos, 98,88 segundos |
| Cobertura de linhas de `app` | **84,63%**, 3.773 de 4.458 linhas executáveis |
| Ruff | Nenhum erro |
| Testes das regras de estado da interface | Passaram |
| TypeScript e build de produção com Vite 7.3.7 | Passaram |
| `npm audit` após atualização | Nenhuma vulnerabilidade reportada no momento da execução |
| Migrações em SQLite vazio | Sete migrações aplicadas; revisão final `d1e2f3a4b5c6` |
| Estrutura migrada | Tabelas de usuários, câmeras, alertas e eventos; campos de rotação, zona e portaria presentes |
| Verificação de whitespace do diff | Passou |

O teste de vídeo usa um arquivo pequeno gerado localmente e o decoder do
OpenCV. Os testes de regras e serviços usam cenários controlados. Isso valida
regressões de software, mas não substitui avaliação dos modelos em imagens
industriais reais.

Não foram executados nesta etapa: benchmark das cinco câmeras físicas,
inferência com os pesos usados pelos colegas, chamada real de segunda opinião
com credenciais, build/execução do container e inspeção visual completa em
navegador. Não houve publicação, push nem alteração na planta.

## Como repetir as verificações

Em um ambiente Python preparado com as dependências do projeto:

```powershell
python -m pytest -q --basetemp=.validation-tests --disable-warnings --tb=short
ruff check .
cd frontend
npm ci
npm test
npm run build
npm audit
```

O frontend requer Node `^20.19.0 || >=22.12.0`. A atualização segue a
[migração oficial do Vite 7](https://v7.vite.dev/guide/migration).
Saídas locais de validação e caches são ignorados pelo Git. O bundle servido
pelo Flask foi reconstruído e acompanha as alterações de interface.

## Critérios para o piloto e para a competição

Antes de declarar o piloto pronto, precisamos de evidência de funcionamento
contínuo das cinco fontes e de precisão por EPI. Um conjunto de cenas anotadas
deve incluir pessoas sobrepostas, diferentes distâncias, oclusão, iluminação,
EPIs parecidos e câmera desconectada. Separar cenas de ajuste das cenas de
avaliação evita apresentar como generalização um resultado calibrado no mesmo
material.

Para cada classe, registrar falsos positivos e falsos negativos, com atenção a
luvas, óculos, máscara e calçado em pessoas distantes. Manter a situação
“indisponível/não verificado” quando o modelo ou a cena não permite confirmar
um EPI. O reconhecimento de um alerta e a marcação como falso positivo são
ações operacionais; não são treinamento automático do modelo.

Também é necessário definir uma meta mensurável de latência e duração do teste
contínuo, verificar recuperação após falha e conferir persistência ao reiniciar
o ambiente de execução. Credenciais reais devem ficar na configuração local,
e as credenciais de demonstração do banco precisam ser substituídas antes do
uso na planta.

Para a apresentação, a melhoria mais forte será demonstrar simultaneamente
as cinco câmeras, uma falha recuperável e uma infração conhecida, mostrando
tempos medidos e o estado correto da interface. Esta rodada entrega a base de
software corrigida e verificável para fazer essa avaliação sem confundir
ausência de alerta com funcionamento correto.
