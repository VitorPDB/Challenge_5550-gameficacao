# Jornada de prevenção

A entrada **Jornada** na barra principal apresenta a gamificação para todos os
perfis. A tela inicial do Supervisor também tem um cartão dedicado. O link
`/dashboard#journey` abre essa tela diretamente após autenticação. Técnico e
Supervisor selecionam o setor; o Operador continua limitado à sua câmera.
A aba **Missões**, no menu **Mais** da câmera, e o kiosk mantêm os acessos locais.
A interface usa os estilos existentes,
com barras de progresso e conquistas; motion design permanece para outra etapa.

## Regras de progressão

Cada ação preventiva verificada concede **100 XP** à equipe naquela câmera.
Ações abertas ou enviadas para revisão não concedem pontos. Metas e conquistas
reconhecem o progresso, sem pontos extras. Não há penalidade por alertas,
ranking de infrações, recompensa por câmera desligada ou pontuação por clicar
em reconhecer um alerta.

| Nível | Nome | XP acumulado necessário |
| --- | --- | --- |
| 1 | Primeiros passos | 0 |
| 2 | Equipe atenta | 300 |
| 3 | Equipe preventiva | 800 |
| 4 | Guardiões do setor | 1.500 |
| 5 | Referência em prevenção | 2.500 |

Os títulos representam participação em ações preventivas verificadas, não uma
certificação de segurança ou prova de redução de acidentes. A interface mostra
XP total, pontos restantes para o próximo nível e ações por situação.

## Conquistas e metas

Seis conquistas podem ser desbloqueadas:

- Primeira ação preventiva: uma ação verificada.
- Prevenção em três frentes: ações em três categorias reconhecidas.
- Prevenção em prática: dez ações verificadas.
- Olhos da prevenção: uma ação de cobertura de câmera verificada.
- Cuidado com a postura: uma ação de ergonomia verificada.
- Semana de ação: três ações verificadas em um intervalo de até sete dias.

As conquistas permanecem desbloqueadas após cumprir os critérios. As duas metas
dinâmicas mostram o progresso dos **últimos sete dias**, uma janela móvel:
três ações verificadas e ações em duas categorias diferentes. O progresso dessas
metas pode diminuir quando ações saem da janela, sem remover conquistas já
obtidas. Repetir a mesma categoria não desbloqueia diversidade.

## Fluxo real

1. Técnico ou Supervisor cria uma missão com equipe, categoria e título.
2. A equipe executa a ação. Um usuário autorizado da câmera descreve a evidência.
3. Outro usuário com papel de Supervisor verifica ou devolve para revisão.
4. Uma verificação válida libera 100 XP e recalcula nível, metas e conquistas.

O mesmo usuário que enviou a evidência não pode verificar sua própria ação.
Repetir a chamada de revisão não duplica pontos. Descrições são evidências
declaradas; cabe ao supervisor confirmar que a ação aconteceu.

Sugestões de missão vêm das até 500 ocorrências mais recentes dos últimos sete
dias, excluindo falsos positivos. Elas são sugestões, não missões concluídas
automaticamente. Categorias são controladas pelo servidor. Nomes de equipe com
diferenças de espaços ou maiúsculas reutilizam o cadastro lógico existente na
mesma câmera; não dividem o progresso em equipes artificiais. Uma missão igual
já aberta ou aguardando revisão é rejeitada.

## Persistência e limites

O banco armazena missões, evidência, autores, revisão e datas. XP e conquistas
são derivados dessas ações, sem saldo mutável enviado pelo navegador. Assim,
recarregar a página ou reiniciar o servidor mantém o progresso. O cálculo usa
todas as missões daquela câmera; a lista visual mostra até 200 recentes.

A identidade de equipe nesta etapa é o nome normalizado **por câmera**. Não é
um cadastro global com membros ou integração de RH, nem pontuação individual
dos colaboradores filmados. Perfil, evidências e consultas respeitam o escopo
de acesso da câmera. Nenhuma missão do laboratório concede XP.

O banco existente precisa da migração de prevenção:

```powershell
python -m flask --app wsgi db upgrade
```

Os testes verificam fronteiras dos níveis, conquistas, janela móvel, persistência
lógica, datas SQLite, separação de equipes, categorias e revisão sem duplicação.

Validação final: **466 testes Python passaram** (3 avisos), Ruff sem erros,
testes do frontend e build TypeScript/Vite aprovados. A inspeção visual em
navegador ficou pendente após bloqueio da revisão automática por limite de uso.
