# -*- coding: utf-8 -*-


import logging
from celery import Celery, shared_task
from datetime import datetime, timedelta

from procapi.utils.services import ConsultaEProcMovimentados, ConsultaEProc
from procapi.processo.models import (
    Assunto,
    Classe,
    Evento,
    EventoDocumento,
    Localidade,
    OrgaoJulgador,
    Parte,
    ParteAdvogado,
    PartePessoa,
    PartePessoaEndereco,
    Processo,
    ProcessoBruto,
    ProcessoAssunto,
    ProcessoClasse,
    ProcessoLocalidade,
    ProcessoOrgaoJulgador,
    ProcessoVinculado,
    TipoDocumento
    )

app = Celery('procapi_tasks')
app.config_from_object('django.conf:settings', namespace='CELERY')

from config.settings.common import mongo_conn

logger = logging.getLogger(__name__)


@shared_task
def consultar_processos_movimentados_periodo(grau, periodo, max_registros=None, pagina=None):
    """Baixa o número dos processos movimentos em um intervalo de tempo"""

    data  = datetime.now()
    data_final = datetime(data.year, data.month, data.day, data.hour, data.minute)
    data_inicial = data_final-timedelta(minutes=periodo)

    return consultar_processos_movimentados(
        grau=grau,
        data_inicial=data_inicial,
        data_final=data_final,
        max_registros=max_registros,
        pagina=pagina
    )


@shared_task
def consultar_processos_movimentados(grau, data_inicial, data_final, max_registros=None, pagina=None):
    """Baixa o número dos processos movimentos em um intervalo de datas"""

    if not isinstance(data_inicial, datetime) and not isinstance(data_final, datetime):
        data_inicial = datetime.strptime(data_inicial, "%Y-%m-%d %H:%M:%S")
        data_final = datetime.strptime(data_final, "%Y-%m-%d %H:%M:%S")

    consulta = ConsultaEProcMovimentados(
        grau=grau,
        data_inicial=data_inicial,
        data_final=data_final,
        max_registros=max_registros)

    resposta = consulta.consultar(pagina=pagina)

    if resposta:
        for processo in resposta:
            criar_processo_movimentado.delay(numero=processo)
        return True
    else:
        #Não conseguiu consultar. Repetir metodo?
        return False


@shared_task
def criar_processo_movimentado(numero):
    """Cria processo movimentado ou marca como desatualizado"""
    processo = Processo.objects.filter(numero=numero).first()
    novo = False

    if processo:
        processo.atualizado = False
        processo.save()
    else:
        novo = True
        processo = Processo.objects.create(numero=numero, atualizado=False)
    
    return {"numero": processo.numero, "novo": novo}


@shared_task
def atualizar_processos_desatualizados(limite=None):
    """Recupera lista de processos desatualizados para atualização individual"""

    processos = Processo.objects.filter(atualizado=False).values_list('numero')

    if limite:
        processos = processos[:limite]

    msg = '{} processos serão atualizados'.format(len(processos))

    for processo in processos:
        atualizar_processo_desatualizado.delay(numero=processo)


@shared_task
def atualizar_processo_desatualizado(numero):
    """Atualiza informações de um processo consultando serviço externo
    1 - verifica se processo existe no banco de dados
    2 - se não existe, cria processo desatualizado
    3 - consulta serviço externo
    4 - registra consulta na tabela de consultas
    5 - se resposta sucesso, cria processo bruto
    6 - se resposta sucesso, chama 'extrair_dados_processo_bruto'
    """

    processo = Processo.objects.filter(numero=numero).first()

    consulta = ConsultaEProc()

    if consulta.consultar(numero):

        eproc = ProcessoBruto.objects.filter(processo=processo).first()

        if eproc:
            eproc.__dict__.update(consulta.resposta_to_dict())
            eproc.save()
        else:
            eproc = ProcessoBruto.objects.create(processo=processo, **consulta.resposta_to_dict())

        extrair_eventos_processo_bruto(eproc)
        extrair_partes_processo_bruto(eproc)
        extrair_cabecalho_processo_bruto(eproc)

        return "Processo {} atualizado".format(numero)

    else:

        return "Erro ao atualizar processo {}: {}".format(numero,
            consulta.mensagem)


def extrair_cabecalho_processo_bruto(eproc):
    """Atualizar dados cabecalho do processo apartir da extração dos dados brutos armazenados"""
    processo = eproc.processo

    # Classe
    classe = Classe.objects.filter(codigo=eproc.dadosBasicos.get('_classeProcessual')).first()

    if classe:
        processo.classe = ProcessoClasse(codigo=classe.codigo, nome=classe.nome)
    else:
        processo.classe = None

    # Localidade
    localidade = Localidade.objects.filter(codigo=eproc.dadosBasicos.get('_codigoLocalidade')).first()

    if localidade:
        processo.localidade = ProcessoLocalidade(codigo=localidade.codigo, nome=localidade.nome)
    else:
        processo.localidade = None

    # Orgão
    orgao = OrgaoJulgador.objects.filter(codigo=eproc.dadosBasicos.get('_codigoOrgaoJulgador')).first()

    if orgao:
        processo.orgao_julgador = ProcessoOrgaoJulgador(codigo=orgao.codigo, nome=orgao.nome)
    else:
        processo.orgao_julgador = None

    # Assuntos
    processo.assuntos = None
    if 'assunto' in eproc.dadosBasicos:
        for item in eproc.dadosBasicos['assunto']:
            assunto = Assunto.objects.filter(codigo=item.get('codigoNacional')).first()
            if assunto:
                processo.assuntos.append(
                    ProcessoAssunto(
                        codigo=assunto.codigo,
                        nome=assunto.nome
                    )
                )

    # Vinculados
    processo.vinculados = None
    if 'processoVinculado' in eproc.dadosBasicos:
        processo.vinculados.exclude()
        for item in eproc.dadosBasicos.get('processoVinculado'):
            processo.vinculados.append(
                ProcessoVinculado(
                    numero=item.get('_numeroProcesso'),
                    vinculo=item.get('_vinculo')
                )
            )

    # Outros
    # processo.grau = consulta.grau(processo.numero)
    processo.nivel_sigilo = eproc.dadosBasicos['_nivelSigilo']
    processo.valor_causa = eproc.dadosBasicos['valorCausa']
    processo.data_ultimo_movimento = None
    processo.data_ultima_atualizacao = datetime.now()
    processo.atualizado = True
    processo.atualizando = False

    processo.save()

    return "Cabecalho do processo {} extraído com sucesso!".format(processo.numero)


def extrair_eventos_processo_bruto(eproc):
    """Atualizar eventos do processo apartir da extração dos dados brutos armazenados"""
    processo = eproc.processo

    documentos = {}
    for item in eproc.documento:
        documentos[item.get('_idDocumento')] = item

    tipos_documentos = TipoDocumento.objects.filter(grau=processo.grau).values_list('codigo', 'nome')
    tipos_documentos = dict((x,y) for x, y in tipos_documentos)

    for item in eproc.movimento:

        if not Evento.objects.filter(processo=processo, numero=item.get('_identificadorMovimento')).count():

            evento = Evento(
                processo=processo,
                numero=item.get('_identificadorMovimento'),
                data_protocolo=datetime.strptime(item.get('_dataHora'), '%Y%m%d%H%M%S'),
                nivel_sigilo = item.get('_nivelSigilo'),
                tipo_local = item.get('_identificadorMovimentoLocal'),
                usuario = item.get('_identificadorUsuarioMovimentacao'),
                descricao = item.get('movimentoLocal')
            )

            evento.documentos = None

            if 'idDocumentoVinculado' in item:
                for documento in item['idDocumentoVinculado']:
                    documento = documentos[documento]
                    documento = EventoDocumento(
                        documento=documento.get('_idDocumento'),
                        tipo=documento.get('_tipoDocumento'),
                        nome=tipos_documentos[int(documento.get('_tipoDocumento'))] if int(documento.get('_tipoDocumento')) in tipos_documentos else None,
                        mimetype=documento.get('_mimetype')
                    )
                    evento.documentos.append(documento)
                if evento.usuario[:2].upper() == 'DP':
                    evento.defensoria = True
            evento.save()

    return "Eventos do processo {} extraídos com sucesso!".format(processo.numero)


def extrair_partes_processo_bruto(eproc):
    """Atualizar partes do processo apartir da extração dos dados brutos armazenados"""
    processo = eproc.processo

    Parte.objects.filter(processo=processo).delete()

    for polo in eproc.dadosBasicos['polo']:

        for item in polo['parte']:

            parte = Parte(
                processo=processo,
                tipo=polo.get('_polo'),
            )

            pessoa = item.get('pessoa')

            parte.pessoa = PartePessoa(
                tipo=pessoa.get('_tipoPessoa'),
                documento_principal=pessoa.get('_numeroDocumentoPrincipal'),
                nome=pessoa.get('_nome'),
                nome_genitor=pessoa.get('_nomeGenitor'),
                nome_genitora=pessoa.get('_nomeGenitora'),
                data_nascimento=datetime.strptime(pessoa.get('_dataNascimento'), '%Y%m%d') if pessoa.get('_dataNascimento') else None,
                data_obito=datetime.strptime(pessoa.get('_dataObito'), '%Y%m%d') if pessoa.get('_dataObito') else None,
                sexo=pessoa.get('_sexo') if pessoa.get('_sexo') else None,
                cidade_natural=pessoa.get('_cidadeNatural'),
                estado_natural=pessoa.get('_estadoNatural'),
                nacionalidade=pessoa.get('_nacionalidade'),
            )

            if 'endereco' in pessoa:
                for endereco in pessoa['endereco']:
                    parte.pessoa.enderecos.append(
                        PartePessoaEndereco(
                            cep=endereco.get('_cep'),
                            logradouro=endereco.get('logradouro'),
                            numero=endereco.get('numero'),
                            complemento=endereco.get('complemento'),
                            bairro=endereco.get('bairro'),
                            cidade=endereco.get('cidade'),
                            estado=endereco.get('estado'),
                            pais=endereco.get('pais')
                        )
                    )

            if 'advogado' in item:
                for advogado in item['advogado']:
                    parte.advogados.append(
                        ParteAdvogado(
                            nome=advogado.get('_nome'),
                            documento_principal=advogado.get('_numeroDocumentoPrincipal'),
                            identidade_principal=advogado.get('_identidadePrincipal'),
                            tipo_representante=advogado.get('_tipoRepresentante')
                        )
                    )

            parte.save()

    return "Partes do processo {} extraídas com sucesso!".format(processo.numero)
