# Prevenção para o NEXT

Implementação local sobre `feat/completo-com-rebrand`, na branch
`codex/rebrand-improvements`. Esta etapa adiciona funcionalidades e controles
operacionais; o refinamento visual e motion design ficam para a próxima etapa.

## O que está integrado

- Previsão de movimento por pessoa, com filtro de Kalman de velocidade
  constante, histórico recente e timestamps de observação. Usa o ponto inferior
  central da caixa como aproximação dos pés.
- Zonas independentes por câmera, com nome, polígono, EPIs exigidos e opção de
  restrição mesmo quando os EPIs foram confirmados.
- Previsão de cruzamento do limite da zona e tempo estimado até a entrada;
  entrada já observada é uma ocorrência diferente da aproximação prevista.
- Convergência entre pessoas: movimento relativo e menor separação projetada
  dentro do horizonte configurado. Distância relativa à imagem e à altura das
  caixas, sem afirmar distância física ou colisão inevitável.
- Exposição acumulada à postura suspeita em uma janela móvel, por identidade
  temporária. Soma apenas intervalos com observações válidas consecutivas de
  postura suspeita; perda de acompanhamento reinicia esse histórico.
- Qualidade da cobertura: imagem escura, excessivamente clara, baixo contraste
  e pessoa pequena para análise detalhada. Confirma degradação persistente após
  um segundo, usando observações novas.
- Explicação visual: zonas, histórico de posição, seta de projeção e elipse de
  incerteza. Alertas carregam motivo, zona, pessoa, ETA, EPIs não confirmados e
  orientação, conforme a regra.
- Missões de equipe com criação, envio de evidência textual e revisão por
  supervisor. Cem pontos por ação verificada e conquistas por primeira ação e
  diversidade de categorias. Sugestões usam ocorrências recentes, excluindo as
  marcadas como falso positivo; não pontuam ausência de alertas.
- Laboratório com MP4 local, processamento real pelo detector configurado,
  comparação de duas configurações sobre as mesmas detecções, imagens de
  evidência e relatório JSON. Execuções podem ser reabertas na interface.

Os novos avisos entram no ciclo existente de alertas, com persistência,
histerese, evidências, reconhecimento, falso positivo e isolamento por câmera.
O laboratório não aciona esse ciclo nem a segunda opinião LLM e não concede
pontos. Os modelos e o lock de inferência são compartilhados; tracking, filtros
e histórico do laboratório são separados dos workers operacionais.

## Como usar

1. Em um ambiente já instalado, faça backup do banco e execute a migração:

   ```powershell
   python -m flask --app wsgi db upgrade
   ```

   A nova revisão é `e2f3a4b5c6d7`. Ela adiciona a configuração à câmera e cria
   tabelas de missões e de execuções do laboratório. `db.create_all()` sozinho
   não atualiza as colunas de um banco existente.

2. Abra uma câmera com perfil Técnico ou Supervisor. No menu de abas **Mais**,
   abra **Prevenção**. Configure horizonte, proximidade relativa e exposição.
   Desenhe cada zona clicando nos vértices, informe requisitos e adicione-a.
   Clique em **Salvar prevenção** para persistir e aplicar. Alterar a
   configuração reinicia o histórico temporal dessa câmera.
3. Inicie a câmera. Observe as projeções no vídeo e os resultados na aba.
   Recursos de zona respeitam `risk_area`; exposição exige `pose` e `posture`.
   Os novos recursos também possuem interruptores próprios.
4. Em **Missões**, informe equipe e ação ou aproveite uma sugestão. O operador
   da câmera pode enviar a descrição da ação realizada. Outro usuário com
   papel de Supervisor verifica ou devolve para revisão. Repetir uma chamada
   de verificação não duplica pontos.
5. Para testar sem fontes IP, salve zonas correspondentes ao enquadramento do
   vídeo e abra **Laboratório**. Envie um MP4 e escolha o horizonte alternativo.
   A câmera não precisa estar rodando. O vídeo é corrigido pela rotação da
   câmera; desenhe as zonas na orientação corrigida.
6. Compare entradas observadas, entradas antecipadas, antecedências e episódios
   de aviso sem entrada observada no horizonte. Inspecione amostras e exporte
   o JSON. Resultados anteriores estão no seletor de execuções recentes.

O Operador vê os resultados e as missões no kiosk, sem controles de
configuração nem acesso ao laboratório. A portaria mantém sua interface
dedicada; configuração e resultados continuam disponíveis ao Técnico e ao
Supervisor nas abas da câmera.

## Limites e significado dos resultados

As coordenadas são normalizadas no plano da imagem. Não há calibração de piso
ou estimativa em metros. A projeção presume continuidade do movimento, não
intenção humana. A elipse representa uma margem heurística de incerteza, não
uma probabilidade calibrada. Histórico insuficiente, movimento quase parado,
resíduo alto e perda de observação impedem a projeção.

O uso de Kalman segue a formulação documentada na
[referência oficial do OpenCV](https://docs.opencv.org/4.x/dd/d6a/classcv_1_1KalmanFilter.html).
Os testes de movimento verificam comportamento controlado; não demonstram
precisão de previsão em trajetórias industriais reais.

A identidade é a do tracker **durante a sessão de uma câmera**. Não foi
implementada associação a cadastro de colaborador, identidade entre câmeras
ou biometria. Reiniciar ou perder a pessoa pode trocar o identificador. A
exposição é um indicador técnico de observações de postura, não RULA/REBA
validado ou diagnóstico médico. Não acumula horas de um colaborador identificado.

Qualidade visual usa heurísticas. Baixo contraste pode sugerir obstrução, mas
não comprova lente obstruída. Uma imagem considerada utilizável não comprova
precisão dos EPIs ou ausência de risco.

Os EPIs da zona precisam ter status `ok` para serem confirmados. Ausente,
incorreto, não suportado e desativado são tratados como **não confirmados**,
sem transformar todos esses estados em uma afirmação de ausência física.

Missões reconhecem ações verificadas. Não treinam o detector, não reduzem
automaticamente o risco e não provam acidentes evitados. Sugestões consultam
até 500 ocorrências dos últimos sete dias. A lista exibe até 200 missões;
a pontuação considera todas as ações verificadas daquela câmera.

## Laboratório: limites e evidências

O laboratório usa uma tarefa por vez, até 128 MB por arquivo, 120 segundos de
vídeo, 600 observações e dez minutos de tempo de processamento entre frames.
Não interrompe uma chamada de inferência já em execução. Amostra até cinco
vezes por segundo e reduz a imagem para dimensão máxima de 1280 pixels. Para
comparabilidade, mantém as duas configurações sobre as mesmas detecções.

Os pesos configurados precisam existir e funcionar no ambiente. O laboratório
compartilha inferência com os workers: executá-lo durante o monitoramento pode
reduzir desempenho. O relatório indica quando o processamento foi limitado.
O arquivo fonte enviado é removido ao concluir ou falhar; amostras e relatório
ficam em `runtime/scenarios`, acessíveis somente a perfis autorizados.
Um reinício marca tarefas interrompidas como falhas; elas não ficam fingindo
processamento ativo. O caminho de armazenamento é configurável por
`SCENARIO_DIR` na configuração da aplicação.

Entradas e avisos do relatório vêm do detector/tracker, não de anotação humana.
Assim, ele não fornece mAP, precisão ou recall do modelo. Avisos no fim do
vídeo, sem horizonte futuro completo, são marcados como inconclusivos em vez
de falsos alarmes. Para validação científica, é necessário anotar manualmente
um conjunto separado e comparar os resultados com essa referência.

## Verificação e próxima avaliação

Os testes adicionados cobrem projeção antes de entrada, passagem ao lado,
pessoa parada, cruzamento de zona estreita, perda de acompanhamento, frames
reaproveitados, exposição por pessoa, convergência, degradação visual,
validação de configuração, isolamento entre câmeras, permissões, missões,
reprodução pelo decoder OpenCV e ausência de escrita operacional no laboratório.

A migração foi testada em banco temporário com upgrade, downgrade e novo
upgrade. O build TypeScript/Vite e os testes do frontend foram executados.
A suíte final, incluindo a jornada de gamificação, passou com **466 testes**
e três avisos. Ruff, testes do frontend e build TypeScript/Vite passaram.
A inspeção visual em navegador ficou pendente após bloqueio da revisão
automática por limite de uso; não foi contornada.

Antes de apresentar como capacidade industrial comprovada: validar cenas de
aproximação e desistência, cruzamentos de pessoas e oclusão; medir antecedência
e avisos indevidos com anotação independente; ajustar limiares ao enquadramento;
medir o custo adicional nas cinco câmeras e repetir o ensaio contínuo de 30 min.

Não houve mudança de pesos, publicação nem acesso às câmeras da planta.
