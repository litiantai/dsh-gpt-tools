"""可选真实工具链验收；显式提供临时工具目录，不读取用户 Maven/Gradle 缓存。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot import quality
from autopilot.workspace import git


@unittest.skipUnless(os.environ.get('DSH_WORKFLOW_TOOLCHAINS'), '需要显式 DSH_WORKFLOW_TOOLCHAINS 才运行真实 JVM 集成测试')
class ToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsh-workflow-real-')
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        self.tools = Path(os.environ['DSH_WORKFLOW_TOOLCHAINS'])
        self.env = patch.dict(os.environ, {'JAVA_HOME':str(self.tools/'amazon-corretto-21.jdk/Contents/Home')})
        self.env.start()
        (self.repo/'.gitignore').write_text('target/\nbuild/\n.gradle/\n')
        src = self.repo/'src/main/java'; src.mkdir(parents=True)
        (src/'Example.java').write_text('''/**
 * 编译和执行验收样例。
 * @author 李天泰 <litiantai@myhexin.com>
 * @date 2026/10/10
 */
public class Example {
    /**
     * 校验示例业务结果。
     * @param args 命令行参数
     * @author 李天泰 <litiantai@myhexin.com>
     * @date 2026/10/10
     */
    public static void main(String[] args) {
        if (6 * 7 != 42) throw new AssertionError("wrong result");
        System.out.println("REAL_JAVA_ASSERTION_PASSED");
    }
}
''')
        git(self.repo, 'init', '-b', 'main');git(self.repo,'config','user.name','Test');git(self.repo,'config','user.email','test@localhost')

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_quality(self, config):
        git(self.repo, 'add', '.');git(self.repo,'commit','-m','toolchain fixture')
        result = quality.run(self.repo, config, self.root/'evidence', local=False)
        self.assertEqual(result['status'], 'pass', json.dumps(result, ensure_ascii=False)[-6500:])
        self.assertEqual([c['phase'] for c in result['checks']], ['install','compile','test'])
        quality.validate(result['quality'], self.repo, config, trusted_root=self.root, phases=quality.PHASES[:-1])
        self.assertIn('REAL_JAVA_ASSERTION_PASSED', result['checks'][-1]['log_tail'])
        self.assertTrue('-o' in result['checks'][1]['command'] or '--offline' in result['checks'][1]['command'])

    def test_maven_dependency_resolution_compile_and_execution(self):
        (self.repo/'pom.xml').write_text('''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>fixture</groupId><artifactId>workflow</artifactId><version>1</version>
<properties><maven.compiler.release>17</maven.compiler.release></properties>
<build><plugins>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-dependency-plugin</artifactId><version>3.8.1</version></plugin>
<plugin><groupId>org.codehaus.mojo</groupId><artifactId>exec-maven-plugin</artifactId><version>3.5.0</version><configuration><mainClass>Example</mainClass></configuration></plugin>
</plugins></build></project>''')
        mvn = str(self.tools/'apache-maven-3.9.9/bin/mvn')
        self.run_quality({'stack':'java','install_policy':'jvm-resolve', 'command_timeout':600,
            'commands':{'install':[[mvn,'dependency:go-offline']], 'compile':[[mvn,'compile']], 'test':[[mvn,'exec:java']]}})

    def test_gradle_dependency_resolution_compile_and_execution(self):
        (self.repo/'settings.gradle').write_text("rootProject.name = 'workflow-fixture'\n")
        (self.repo/'build.gradle').write_text('''plugins { id 'java' }
tasks.register('autopilotResolveDependencies') {
    doLast { configurations.findAll { it.canBeResolved }.each { it.resolve() } }
}
tasks.register('verifyFixture', JavaExec) {
    dependsOn classes
    classpath = sourceSets.main.runtimeClasspath
    mainClass = 'Example'
}
''')
        gradle = str(self.tools/'gradle-8.14.3/bin/gradle')
        self.run_quality({'stack':'java','install_policy':'jvm-resolve','command_timeout':120,
            'commands':{'install':[[gradle,'autopilotResolveDependencies']], 'compile':[[gradle,'classes']], 'test':[[gradle,'verifyFixture']]}})


if __name__ == '__main__':
    unittest.main()
