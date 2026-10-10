// Parse trusted role workflow resources; executable YAML tags and aliases are unsupported.
import {parseDocument} from 'yaml';
let source = '';
for await (const chunk of process.stdin) source += chunk;
const doc = parseDocument(source, {schema: 'core', uniqueKeys: true});
if (doc.errors.length || doc.warnings.length) throw new Error([...doc.errors, ...doc.warnings].map(e => e.message).join('\n'));
process.stdout.write(JSON.stringify(doc.toJS({maxAliasCount: 0})));
