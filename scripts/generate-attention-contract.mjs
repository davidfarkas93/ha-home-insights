// Maintainer-only oracle: no JavaScript runtime is used by the HA integration.
import {readFileSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import vm from 'node:vm';
const luma=resolve(process.argv[2]);
const require=createRequire(join(luma,'package.json')),ts=require('typescript');
let clock=0;class ClockDate extends Date{static now(){return clock;}}
const modules={};
for(const name of ['incidents','localize','household-context','attention']){
 const exports={};
 const source=ts.transpileModule(readFileSync(join(luma,'src',name+'.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(source,{exports,require:name=>modules[name.slice(2)],Date:ClockDate});modules[name]=exports;
}
const cases=JSON.parse(execFileSync('.venv/bin/python',['tests/attention_cases.py'],{encoding:'utf8'})),expected={};
for(const [name,c] of Object.entries(cases)){clock=c.now;expected[name]=modules.attention.attentionSnapshot({states:c.states,entities:c.registry,locale:{language:c.language}},c.policy,c.external,c.now);}
writeFileSync('tests/attention_expected.json',JSON.stringify({contract:'Luma attention v1',source:'lovelace-luma v0.47.0',expected},null,2)+'\n');
process.stdout.write(`Generated ${Object.keys(expected).length} differential cases\n`);
