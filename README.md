# LeadPilot AI — MVP protegido

Aplicação independente para organizar, prospectar e acompanhar oportunidades comerciais. Não faz parte do GTA Oculto AI.

## Recursos desta versão
- Painel de leads, métricas comerciais e prioridades de acompanhamento.
- Cadastro/edição, pesquisa, filtros de status e prevenção de duplicatas.
- Pontuação inicial heurística baseada nos dados disponíveis.
- Prospecção por segmento e cidade usando dados públicos do OpenStreetMap.
- Modo de revisão antes da importação e modo automático para cadastrar resultados elegíveis.
- Pesquisa de contatos comerciais públicos em segundo plano quando disponível; os dados precisam ser conferidos antes de uma abordagem.
- Fila comercial com rascunhos, registro manual de envio/resposta, notas e agendamento de retornos.
- Filtros separados para retornos atrasados, de hoje e futuros.
- Importação e exportação CSV/XLSX.
- Login obrigatório para as páginas comerciais e persistência em PostgreSQL.

## Limites importantes
- O aplicativo não usa uma API externa de IA no momento. “Automático” significa executar etapas predefinidas de busca, deduplicação e cadastro, não uma IA autônoma.
- A cobertura e a qualidade dos resultados dependem dos dados públicos disponíveis. Nem todas as empresas têm site, telefone ou e-mail públicos.
- Não envia e-mails, WhatsApp ou outras mensagens automaticamente.
- A pontuação é heurística; revise os dados e os rascunhos antes de abordar qualquer empresa.
- O aplicativo não garante conversões nem receita. O resultado comercial precisa ser validado com contatos reais.
- Ao usar dados pessoais para prospecção, verifique a base legal, a finalidade, a minimização dos dados e os mecanismos de oposição aplicáveis à LGPD.

## Configuração obrigatória
O aplicativo deve falhar ao iniciar se as variáveis essenciais não estiverem definidas, evitando publicar uma versão sem autenticação ou banco.

- `DATABASE_URL`: URL de conexão do PostgreSQL.
- `SECRET_KEY`: segredo forte e aleatório para sessões Flask.
- `APP_USERNAME`: usuário de acesso.
- `APP_PASSWORD_SHA256`: hash SHA-256 hexadecimal da senha, não a senha em texto puro.
- `COOKIE_SECURE=1` em produção HTTPS.

Gere o hash da senha com Python:

```bash
python -c "import getpass,hashlib; print(hashlib.sha256(getpass.getpass('Senha: ').encode()).hexdigest())"
```

## Publicação no Render
1. Crie um PostgreSQL para o LeadPilot AI.
2. Crie um Web Service ligado exclusivamente a `gtaocultooficial-sudo/leadpilot-ai`, nunca ao repositório `gta-oculto-ai`.
3. Build: `pip install -r requirements.txt`
4. Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
5. Configure as variáveis acima antes do deploy. Use a URL interna do banco PostgreSQL quando o banco e o serviço estiverem no Render.

## Próximos passos recomendados
1. Testes automatizados para filtros, deduplicação e fluxos de prospecção.
2. Verificação de robustez dos trabalhos em segundo plano em reinícios do serviço.
3. Medição da qualidade dos contatos e do funil comercial.
4. Validação de uma oferta comercial e obtenção dos primeiros clientes pagantes.
