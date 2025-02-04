import json
from flask import Flask, jsonify, request
from os import environ, path, mkdir, chdir
from subprocess import run
from glob import glob
from lxml import etree
import logging
from logging.handlers import RotatingFileHandler
from re import sub, findall, finditer
from datetime import datetime, date
app = Flask(__name__)

basedir = path.abspath(path.dirname(__file__))

CJHNT_TEXT_FOLDER = path.join(environ.get('HOME'), 'CJHNT_Texts_editing')
ns = {'tei': 'http://www.tei-c.org/ns/1.0'}

if not app.debug and not app.testing:
    if not path.exists('logs'):
        mkdir('logs')
    file_handler = RotatingFileHandler(path.join(basedir, 'logs/dts_put.log'), maxBytes=10240, backupCount=10)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('dts_put startup')

def get_filename(urn: str):
    texts = [x for x in glob(path.join(CJHNT_TEXT_FOLDER, 'data/**/*.xml'), recursive=True) if '__capitains__' not in x]
    return next((t for t in texts if urn.split(':')[-1] in t), None)

def create_translation_file(edition_urn: str, translation_urn: str, lang: str):
    exemplar_file = get_filename(edition_urn)
    if exemplar_file is None:
        return None
    new_filename = path.join(path.dirname(exemplar_file), translation_urn.split(':')[-1] + '.xml')
    app.logger.info('Creating new translation file: ' + new_filename)
    exemplar_xml = etree.parse(exemplar_file)
    for t in exemplar_xml.xpath('/tei:TEI/tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:title', namespaces=ns):
        t.text = t.text.replace(' (deu)', '').replace(' (eng)', '') + ' ({})'.format(lang)
    for e in exemplar_xml.xpath('/tei:TEI/tei:teiHeader/tei:fileDesc/tei:editionStmt/tei:edition', namespaces=ns):
        e.text = e.text.replace(' (deu)', '').replace(' (eng)', '') + ' ({})'.format(lang)
    for ps in exemplar_xml.xpath('/tei:TEI/tei:teiHeader/tei:fileDesc/tei:publicationStmt', namespaces=ns):
        ps.clear()
        pub = etree.SubElement(ps, 'publisher')
        pub.text = 'CJHNT-Digital Projekt'
        pub_place = etree.SubElement(ps, 'pubPlace')
        pub_place.text = 'Leipzig'
        date_element = etree.SubElement(ps, 'date')
        date_today = str(date.today())
        date_element.set('when', date_today)
        date_element.text = date_today
        avail = etree.SubElement(ps, 'availability')
        license = etree.SubElement(avail, 'licence')
        license.set('target', 'https://creativecommons.org/licenses/by-sa/4.0/')
        license.text = 'Available under a Creative Commons Attribution-ShareAlike 4.0 International License'
    for d in exemplar_xml.xpath('/tei:TEI/tei:text/tei:body/tei:div', namespaces=ns):
        d.clear()
        d.set('type', 'translation')
        d.set('n', translation_urn)
        d.set('{http://www.w3.org/XML/1998/namespace}lang', lang)
        d.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    return (new_filename, exemplar_xml)

def validate_xml(xml: str, urn: str):
    try:
        etree.fromstring(xml)
        return True
    except Exception as E:
        app.logger.error(urn + ': ' + str(E))
        return False
    
def run_git_command(cmd_list: list):
    git_command = run(cmd_list, capture_output=True)
    app.logger.info(git_command.stdout)
    if git_command.stderr:
        app.logger.warning(git_command.stderr)

@app.route('/texts', methods=['GET'])
def get_texts():
    texts = [x for x in glob(path.join(CJHNT_TEXT_FOLDER, 'data/**/*.xml'), recursive=True) if '__capitains__' not in x]
    return jsonify(texts)

@app.route('/texts/<urn>', methods=['PUT'])
def update_text(urn: str):
    '''
    request.data = {
    'editionUrn': the urn for the base edition from which the translation was made, 
    'translationLang': the three-letter language code for the translation (e.g., 'deu' or 'eng'), 
    'translationText': the text of the translation, 
    'citation': the full citation path to the translated text (e.g., I.2.4). Must be separated with periods,
    'user': the email address of the user who made the translation
    }
    '''
    # make sure the request.data can be loaded as JSON
    try:
        request_data = json.loads(request.data)
    except Exception as E:
        app.logger.error('Request Data could not be read: {}\nData: {}'.format(E, request_data))
        return jsonify({'error': 'Data could not be read.', 'data': request.data}), 400
    # try to determine the filename in which the translation should be saved
    filename = get_filename(urn=urn)
    # if no file exists with that filename, then a new translation file should be created
    # its name should be based on the editionUrn on which the translation is based
    if filename is None:
        new_translation_file = create_translation_file(request_data['editionUrn'], urn, request_data['translationLang'])
        if new_translation_file is None:
            app.logger.error('Translation file could not be created for ' + urn)
            return jsonify({'error': 'There is no file for this translation'}), 404
        filename = new_translation_file[0]
        translation_xml = new_translation_file[1]
    else:
        translation_xml = etree.parse(filename)
    ref_levels = translation_xml.xpath('/tei:TEI/tei:teiHeader/tei:encodingDesc/tei:refsDecl/tei:cRefPattern', namespaces=ns)
    cit_levels = request_data['citation'].split('.')
    ref_xpaths = list()
    '''
    This loop fills the ref_xpaths list with dicts like the following:
     {
     'name': the name of the level (e.g., 'chapter', 'verse'),
     'xpath': the xpath to find the translated section in the XML file,
     'number': the @n atttribute of the translated section
     } 
    '''
    for i, cit in enumerate(cit_levels):
        one_index = i + 1
        level_name = ref_levels[-one_index].get('n')
        level_xpath = sub(r'#xpath\((.*)\)', r'\1', ref_levels[-one_index].get('replacementPattern'))
        for m in findall(r'\$(\d)', level_xpath):
            level_xpath = sub(r'\${}'.format(m), cit_levels[int(m) - 1], level_xpath)
        ref_xpaths.append({'name': level_name, 'xpath': level_xpath, 'number': cit})
    # The edition xml is needed to determine the correct order for the sections in the translation
    edition_file = get_filename(request_data['editionUrn'])
    if edition_file is None:
        app.logger.error('The edition for {} could not be found at {}.'.format(request_data['editionUrn'], edition_file))
        return jsonify({'error': 'There is no file for this edition'}), 404
    edition_xml = etree.parse(edition_file)
    # This loop finds the element in each ref_xpaths level, creating it and placing it in the correct document order as necessary
    for xpath in ref_xpaths:
        container = next((x for x in translation_xml.xpath(xpath['xpath'], namespaces=ns)), None)
        if container is None:
            # the container doesn't exist and needs to be created
            parent_xpath = '/'.join(xpath['xpath'].split('/')[:-1])
            print(parent_xpath, xpath['xpath'])
            # find the parent element for the container using a shortened version of the container xpath
            translation_parent_element = next((x for x in translation_xml.xpath(parent_xpath, namespaces=ns)), None)
            edition_parent_element = next((x for x in edition_xml.xpath(parent_xpath, namespaces=ns)), None)
            if translation_parent_element is None:
                app.logger.error('The parent element for xpath {} ({}) in {} could not be found.'.format(xpath, parent_xpath, filename))
                return jsonify({'error': 'Your translation could not be saved. Contact your system administrator for more information'}), 500
            if edition_parent_element is None:
                app.logger.error('The parent element for xpath {} ({}) in {} could not be found.'.format(xpath, parent_xpath, filename))
                return jsonify({'error': 'Your translation could not be saved. Contact your system administrator for more information'}), 500
            element_name = sub(r'tei:(\w+)\[.*', r'\1', xpath['xpath'].split('/')[-1])
            translation_sibling_numbers = translation_parent_element.xpath('{}/tei:{}/@n'.format(parent_xpath, element_name), namespaces=ns) + [xpath['number']]
            edition_sibling_numbers = edition_parent_element.xpath('{}/tei:{}/@n'.format(parent_xpath, element_name), namespaces=ns)
            # find order of siblings in the edition and the translation to discover where to put the new container
            sorted_translation_sibling_numbers = [n for n in edition_sibling_numbers if n in translation_sibling_numbers]
            insert_index = sorted_translation_sibling_numbers.index(xpath['number'])
            container = translation_parent_element.makeelement('{' + ns['tei'] + '}' + element_name, n=xpath['number'], type='textpart', subtype=xpath['name'], nsmap={None: ns['tei']})
            translation_parent_element.insert(insert_index, container)
    # remove all content in the container. Doing a new translation for an already translated verse will replace the translation.
    for child in container:
        container.remove(child)
    # text_ab = etree.SubElement(container, '{' + ns['tei'] + '}ab', nsmap={None: ns['tei']})
    # for match in finditer(r'[\w{]+[^{\w]+', request_data['translationText']):
    #     word = match.group()
    #     w = etree.SubElement(text_ab, '{http://www.tei-c.org/ns/1.0}w', nsmap={None: 'http://www.tei-c.org/ns/1.0'})
    #     w.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    #     w.text = sub(r'\W', '', word)
    #     w.tail = sub(r'[\w\n]', '', word)
    new_element = etree.fromstring(request_data['translationText'])
    container.append(new_element)
    translation_xml.write(filename, encoding='utf-8', pretty_print=True)
    user = request_data['user']
    branch_name = sub(r'\W', '-', urn + '-' + user + str(datetime.today()))
    chdir(CJHNT_TEXT_FOLDER)
    run_git_command(['git', 'checkout', '-b', branch_name])
    run_git_command(['git', 'add', '-A'])
    run_git_command(['git', 'commit', '-m', f'New translation for {urn} by {user}'])
    run_git_command(['git', 'push', 'origin', branch_name])
    run_git_command(['git', 'checkout', 'master'])
    return jsonify({'success': 'The translation was created and is awaiting approval.'}), 201

if __name__ == '__main__':
    app.run(port=5001)