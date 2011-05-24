# The absolute_import directive looks firstly at what packages are available
# on sys.path to avoid name collisions when we import release.* from elsewhere
from __future__ import absolute_import

import os
from buildbot.process.buildstep import regex_log_evaluator
from buildbot.scheduler import Scheduler, Dependent, Triggerable
from buildbot.status.tinderbox import TinderboxMailNotifier
from buildbot.status.mail import MailNotifier
from buildbot.steps.trigger import Trigger

import release.platforms
import release.paths
import buildbotcustom.changes.ftppoller
import build.paths
import release.info
reload(release.platforms)
reload(release.paths)
reload(buildbotcustom.changes.ftppoller)
reload(build.paths)
reload(release.info)

from buildbotcustom.status.mail import ChangeNotifier
from buildbotcustom.misc import get_l10n_repositories, isHgPollerTriggered, \
  generateTestBuilderNames, generateTestBuilder, _nextFastReservedSlave, \
  reallyShort, makeLogUploadCommand
from buildbotcustom.process.factory import StagingRepositorySetupFactory, \
  ScriptFactory, SingleSourceFactory, ReleaseBuildFactory, \
  ReleaseUpdatesFactory, ReleaseFinalVerification, L10nVerifyFactory, \
  PartnerRepackFactory, MajorUpdateFactory, XulrunnerReleaseBuildFactory, \
  TuxedoEntrySubmitterFactory, makeDummyBuilder
from buildbotcustom.changes.ftppoller import UrlPoller, LocalesFtpPoller
from release.platforms import buildbot2ftp, sl_platform_map
from release.paths import makeCandidatesDir
from buildbotcustom.scheduler import TriggerBouncerCheck, makePropertiesScheduler
from buildbotcustom.misc_scheduler import buildIDSchedFunc, buildUIDSchedFunc
from buildbotcustom.status.log_handlers import SubprocessLogHandler
from buildbotcustom.status.errors import update_verify_error
from build.paths import getRealpath
from release.info import getRuntimeTag
import BuildSlaves

DEFAULT_PARALLELIZATION = 10

def generateReleaseBranchObjects(releaseConfig, branchConfig,
                                 releaseConfigFile, sourceRepoKey="mozilla"):
    # This variable is one thing that forces us into reconfiging prior to a
    # release. It should be removed as soon as nothing depends on it.
    sourceRepoInfo = releaseConfig['sourceRepositories'][sourceRepoKey]
    releaseTag = '%s_RELEASE' % releaseConfig['baseTag']
    # This tag is created post-signing, when we do some additional
    # config file bumps
    runtimeTag = getRuntimeTag(releaseTag)
    l10nChunks = releaseConfig.get('l10nChunks', DEFAULT_PARALLELIZATION)
    updateVerifyChunks = releaseConfig.get('updateVerifyChunks', DEFAULT_PARALLELIZATION)
    tools_repo = '%s%s' % (branchConfig['hgurl'],
                           releaseConfig.get('build_tools_repo_path',
                               branchConfig['build_tools_repo_path']))
    config_repo = '%s%s' % (branchConfig['hgurl'],
                             branchConfig['config_repo_path'])

    branchConfigFile = getRealpath('localconfig.py')
    unix_slaves = branchConfig['platforms'].get('linux', {}).get('slaves', []) + \
                branchConfig['platforms'].get('linux64', {}).get('slaves', []) + \
                branchConfig['platforms'].get('macosx', {}).get('slaves', []) + \
                branchConfig['platforms'].get('macosx64', {}).get('slaves', [])
    all_slaves = unix_slaves + \
               branchConfig['platforms'].get('win32', {}).get('slaves', []) + \
               branchConfig['platforms'].get('win64', {}).get('slaves', [])

    if 'signedPlatforms' in releaseConfig.keys():
        signedPlatforms = releaseConfig['signedPlatforms']
    else:
        signedPlatforms = ('win32',)

    def builderPrefix(s, platform=None):
        if platform:
            return "release-%s-%s_%s" % (sourceRepoInfo['name'], platform, s)
        else:
            return "release-%s-%s" % (sourceRepoInfo['name'], s)

    def releasePrefix():
        """Construct a standard format product release name from the
           product name, version and build number stored in release_config.py
        """
        return "%s %s build%s" % (
            releaseConfig['productName'].title(),
            releaseConfig['version'],
            releaseConfig['buildNumber'], )

    def majorReleasePrefix():
        return "%s %s build%s" % (
            releaseConfig['productName'].title(),
            releaseConfig['majorUpdateToVersion'],
            releaseConfig['majorUpdateBuildNumber'], )

    def genericFtpUrl():
        """ Generate an FTP URL pointing to the uploaded release builds for
        sticking into release notification messages """
        return makeCandidatesDir(
            releaseConfig['productName'],
            releaseConfig['version'],
            releaseConfig['buildNumber'],
            protocol='ftp',
            server=releaseConfig['ftpServer'])

    def genericHttpsUrl():
        """ Generate an HTTPS URL pointing to the uploaded release builds for
        sticking into release notification messages """
        return makeCandidatesDir(
            releaseConfig['productName'],
            releaseConfig['version'],
            releaseConfig['buildNumber'],
            protocol='https',
            server=releaseConfig['ftpServer'])

    def createReleaseMessage(mode, name, build, results, master_status):
        """Construct a standard email to send to release@/release-drivers@
           whenever a major step of the release finishes
        """
        msgdict = {}
        releaseName = releasePrefix()
        job_status = "failed" if results else "success"
        allplatforms = list(releaseConfig['enUSPlatforms'])
        xrplatforms = list(releaseConfig['xulrunnerPlatforms'])
        stage = name.replace(builderPrefix(""), "")
        # Detect platform from builder name by tokenizing by '_', and matching
        # the first token after the prefix
        if stage.startswith("xulrunner"):
            platform = ["xulrunner_%s" % p for p in xrplatforms
                if stage.replace("xulrunner_", "").split('_')[0] == p]
        else:
            platform = [p for p in allplatforms if stage.split('_')[0] == p]
        if releaseConfig['majorUpdateRepoPath']:
            majorReleaseName = majorReleasePrefix()
        platform = platform[0] if len(platform) >= 1 else None
        message_tag = releaseConfig.get('messagePrefix', '[release] ')
        buildbot_url = ''
        if master_status.getURLForThing(build):
            buildbot_url = "Full details are available at:\n %s\n" % master_status.getURLForThing(build)
        # Use a generic ftp URL non-specific to any locale
        ftpURL = genericFtpUrl()
        if platform:
            if platform in signedPlatforms:
                platformDir = 'unsigned/%s' % buildbot2ftp(platform)
            else:
                platformDir = buildbot2ftp(platform)
            ftpURL = '/'.join([
                ftpURL.strip('/'),
                platformDir])

        stage = stage.replace("%s_" % platform, "") if platform else stage
        #try to load a unique message template for the platform(if defined, step and results
        #if none exists, fall back to the default template
        possible_templates = ("%s/%s_%s_%s" % (releaseConfig['releaseTemplates'], platform, stage, job_status),
            "%s/%s_%s" % (releaseConfig['releaseTemplates'], stage, job_status),
            "%s/%s_default_%s" % (releaseConfig['releaseTemplates'], platform, job_status),
            "%s/default_%s" % (releaseConfig['releaseTemplates'], job_status))
        template = None
        for t in possible_templates:
            if os.access(t, os.R_OK):
                template = open(t, "r", True)
                break

        if template:
            subject = message_tag + template.readline().strip() % locals()
            body = ''.join(template.readlines())
            template.close()
        else:
            raise IOError("Cannot find a template file to use")
        msgdict['subject'] = subject % locals()
        msgdict['body'] = body % locals() + "\n"
        msgdict['type'] = 'plain'
        return msgdict

    def createReleaseChangeMessage(change):
        """Construct a standard email to send to release@/release-drivers@
           whenever a change is pushed to a release-related branch being
           listened on"""
        msgdict = {}
        releaseName = releasePrefix()
        message_tag = releaseConfig.get('messagePrefix', '[release] ')
        step = None
        ftpURL = genericFtpUrl()
        if change.branch.endswith('signing'):
            step = "signing"
        else:
            step = "tag"
        #try to load a unique message template for the change
        #if none exists, fall back to the default template
        possible_templates = ("%s/%s_change" % (releaseConfig['releaseTemplates'], step),
            "%s/default_change" % releaseConfig['releaseTemplates'])
        template = None
        for t in possible_templates:
            if os.access(t, os.R_OK):
                template = open(t, "r", True)
                break

        if template:
            subject = message_tag + template.readline().strip() % locals()
            body = ''.join(template.readlines()) + "\n"
            template.close()
        else:
            raise IOError("Cannot find a template file to use")
        msgdict['subject'] = subject % locals()
        msgdict['body'] = body % locals()
        msgdict['type'] = 'plain'
        return msgdict

    def createReleaseAVVendorsMessage(mode, name, build, results, master_status):
        """Construct the release notification email to send to the AV Vendors.
        """
        template_name = "%s/updates_avvendors" % releaseConfig['releaseTemplates']
        if not os.access(template_name, os.R_OK):
            raise IOError("Cannot find a template file to use")

        template = open(template_name, "r", True)
        subject = '%(productName)s %(version)s release'
        body = ''.join(template.readlines())
        template.close()

        productName = releaseConfig['productName'].title()
        version = releaseConfig['version']
        buildsURL = genericHttpsUrl()

        msgdict = {}
        msgdict['subject'] = subject % locals()
        msgdict['body'] = body % locals() + "\n"
        msgdict['type'] = 'plain'
        return msgdict

    def parallelizeBuilders(base_name, platform, chunks):
        builders = {}
        for n in range(1, chunks+1):
            builders[n] = builderPrefix("%s_%s/%s" % (base_name, n,
                                                      str(chunks)),
                                        platform)
        return builders

    def l10nBuilders(platform):
        return parallelizeBuilders("repack", platform, l10nChunks)

    def updateVerifyBuilders(platform):
        return parallelizeBuilders("update_verify", platform,
                                   updateVerifyChunks)

    def majorUpdateVerifyBuilders(platform):
        return parallelizeBuilders("major_update_verify", platform,
                                   updateVerifyChunks)

    builders = []
    test_builders = []
    schedulers = []
    change_source = []
    notify_builders = []
    status = []

    shippedLocalesFile = "%s/%s/raw-file/%s/%s" % (
                            branchConfig['hgurl'],
                            sourceRepoInfo['path'],
                            releaseTag,
                            releaseConfig['shippedLocalesPath'])

    ##### Change sources and Schedulers
    if releaseConfig['doPartnerRepacks']:
        for p in releaseConfig['l10nPlatforms']:
            ftpPlatform = buildbot2ftp(p)

            ftpURLs = ["http://%s/pub/mozilla.org/%s/nightly/%s-candidates/build%s/%s" % (
                      releaseConfig['stagingServer'],
                      releaseConfig['productName'],
                      releaseConfig['version'],
                      releaseConfig['buildNumber'],
                      ftpPlatform)]

            if p in signedPlatforms:
                ftpURLs = [
                    "http://%s/pub/mozilla.org/%s/nightly/%s-candidates/build%s/unsigned/%s" % (
                      releaseConfig['stagingServer'],
                      releaseConfig['productName'],
                      releaseConfig['version'],
                      releaseConfig['buildNumber'],
                      ftpPlatform)]

            change_source.append(LocalesFtpPoller(
                branch=builderPrefix("post_%s_l10n" % p),
                ftpURLs=ftpURLs,
                pollInterval=60*5, # 5 minutes
                platform = p,
                localesFile = shippedLocalesFile,
                sl_platform_map = sl_platform_map,
            ))

    change_source.append(UrlPoller(
        branch=builderPrefix("post_signing"),
        url='http://%s/pub/mozilla.org/%s/nightly/%s-candidates/build%s/win32_signing_build%s.log' % (
            releaseConfig['stagingServer'],
            releaseConfig['productName'], releaseConfig['version'],
            releaseConfig['buildNumber'], releaseConfig['buildNumber']),
        pollInterval=60*10,
    ))

    if releaseConfig.get('enable_repo_setup'):
        repo_setup_scheduler = Scheduler(
            name=builderPrefix('repo_setup'),
            branch=sourceRepoInfo['path'],
            treeStableTimer=None,
            builderNames=[builderPrefix('repo_setup')],
            fileIsImportant=lambda c: not isHgPollerTriggered(c,
                branchConfig['hgurl'])
        )
        schedulers.append(repo_setup_scheduler)
        tag_scheduler = Dependent(
            name=builderPrefix('tag'),
            upstream=repo_setup_scheduler,
            builderNames=[builderPrefix('tag')]
        )
        release_downloader_scheduler = Scheduler(
            name=builderPrefix('release_downloader'),
            branch=sourceRepoInfo['path'],
            treeStableTimer=None,
            builderNames=[builderPrefix('release_downloader')],
            fileIsImportant=lambda c: not isHgPollerTriggered(c,
                branchConfig['hgurl'])
        )
        schedulers.append(release_downloader_scheduler)
    else:
        tag_scheduler = Scheduler(
            name=builderPrefix('tag'),
            branch=sourceRepoInfo['path'],
            treeStableTimer=None,
            builderNames=[builderPrefix('tag')],
            fileIsImportant=lambda c: not isHgPollerTriggered(c, branchConfig['hgurl'])
        )

    schedulers.append(tag_scheduler)

    tag_downstream = [builderPrefix('source')]

    if releaseConfig['buildNumber'] == 1:
        tag_downstream.append(builderPrefix('bouncer_submitter'))

        if releaseConfig['doPartnerRepacks']:
            tag_downstream.append(builderPrefix('euballot_bouncer_submitter'))

    if releaseConfig['xulrunnerPlatforms']:
        tag_downstream.append(builderPrefix('xulrunner_source'))

    for platform in releaseConfig['enUSPlatforms']:
        tag_downstream.append(builderPrefix('%s_build' % platform))
        notify_builders.append(builderPrefix('%s_build' % platform))
        if platform in releaseConfig['l10nPlatforms']:
            repack_scheduler = Triggerable(
                name=builderPrefix('%s_repack' % platform),
                builderNames=l10nBuilders(platform).values(),
            )
            schedulers.append(repack_scheduler)
            repack_complete_scheduler = Dependent(
                name=builderPrefix('%s_repack_complete' % platform),
                upstream=repack_scheduler,
                builderNames=[builderPrefix('repack_complete', platform),]
            )
            schedulers.append(repack_complete_scheduler)
            notify_builders.append(builderPrefix('repack_complete', platform))

    for platform in releaseConfig['xulrunnerPlatforms']:
        tag_downstream.append(builderPrefix('xulrunner_%s_build' % platform))

    DependentID = makePropertiesScheduler(Dependent, [buildIDSchedFunc, buildUIDSchedFunc])

    schedulers.append(
        DependentID(
            name=builderPrefix('build'),
            upstream=tag_scheduler,
            builderNames=tag_downstream,
        ))

    if releaseConfig['doPartnerRepacks']:
        for platform in releaseConfig['l10nPlatforms']:
            partner_scheduler = Scheduler(
                name=builderPrefix('partner_repacks', platform),
                treeStableTimer=0,
                branch=builderPrefix('post_%s_l10n' % platform),
                builderNames=[builderPrefix('partner_repack', platform)],
            )
            schedulers.append(partner_scheduler)

    for platform in releaseConfig['l10nPlatforms']:
        l10n_verify_scheduler = Scheduler(
            name=builderPrefix('l10n_verification', platform),
            treeStableTimer=0,
            branch=builderPrefix('post_signing'),
            builderNames=[builderPrefix('l10n_verification', platform)]
        )
        schedulers.append(l10n_verify_scheduler)

    updates_scheduler = Scheduler(
        name=builderPrefix('updates'),
        treeStableTimer=0,
        branch=builderPrefix('post_signing'),
        builderNames=[builderPrefix('updates')]
    )
    schedulers.append(updates_scheduler)
    notify_builders.append(builderPrefix('updates'))

    updateBuilderNames = []
    for platform in sorted(releaseConfig['verifyConfigs'].keys()):
        updateBuilderNames.extend(updateVerifyBuilders(platform).values())
    update_verify_scheduler = Dependent(
        name=builderPrefix('update_verify'),
        upstream=updates_scheduler,
        builderNames=updateBuilderNames
    )
    schedulers.append(update_verify_scheduler)

    check_permissions_scheduler = Dependent(
        name=builderPrefix('check_permissions'),
        upstream=updates_scheduler,
        builderNames=[builderPrefix('check_permissions')]
    )
    schedulers.append(check_permissions_scheduler)

    antivirus_scheduler = Dependent(
        name=builderPrefix('antivirus'),
        upstream=updates_scheduler,
        builderNames=[builderPrefix('antivirus')]
    )
    schedulers.append(antivirus_scheduler)

    if releaseConfig['majorUpdateRepoPath']:
        majorUpdateBuilderNames = []
        for platform in sorted(releaseConfig['majorUpdateVerifyConfigs'].keys()):
            majorUpdateBuilderNames.extend(
                majorUpdateVerifyBuilders(platform).values())
        major_update_verify_scheduler = Triggerable(
            name=builderPrefix('major_update_verify'),
            builderNames=majorUpdateBuilderNames
        )
        schedulers.append(major_update_verify_scheduler)

    for platform in releaseConfig['unittestPlatforms']:
        platform_test_builders = []
        for suites_name, suites in branchConfig['unittest_suites']:
            platform_test_builders.extend(
                    generateTestBuilderNames(
                        builderPrefix('%s_test' % platform),
                        suites_name, suites))

        s = Scheduler(
         name=builderPrefix('%s-opt-unittest' % platform),
         treeStableTimer=0,
         branch=builderPrefix('%s-opt-unittest' % platform),
         builderNames=platform_test_builders,
        )
        schedulers.append(s)

    mirror_scheduler1 = TriggerBouncerCheck(
        name=builderPrefix('ready-for-rel-test'),
        configRepo=config_repo,
        minUptake=10000,
        builderNames=[builderPrefix('ready_for_releasetest_testing')] + \
                      [builderPrefix('final_verification', platform)
                       for platform in releaseConfig['verifyConfigs'].keys()],
        username=BuildSlaves.tuxedoUsername,
        password=BuildSlaves.tuxedoPassword)

    schedulers.append(mirror_scheduler1)

    mirror_scheduler2 = TriggerBouncerCheck(
        name=builderPrefix('ready-for-release'),
        configRepo=config_repo,
        minUptake=45000,
        builderNames=[builderPrefix('ready_for_release')],
        username=BuildSlaves.tuxedoUsername,
        password=BuildSlaves.tuxedoPassword)

    schedulers.append(mirror_scheduler2)

    # Purposely, there is not a Scheduler for ReleaseFinalVerification
    # This is a step run very shortly before release, and is triggered manually
    # from the waterfall

    ##### Builders
    builder_env = {
        'BUILDBOT_CONFIGS': '%s%s' % (branchConfig['hgurl'],
                                      branchConfig['config_repo_path']),
        'BUILDBOTCUSTOM': '%s%s' % (branchConfig['hgurl'],
                                    branchConfig['buildbotcustom_repo_path']),
        'CLOBBERER_URL': branchConfig['base_clobber_url']
    }

    if releaseConfig.get('enable_repo_setup'):
        if not releaseConfig.get('skip_repo_setup'):
            clone_repositories = dict()
            # The repo_setup builder only needs to the repoPath, so we only
            # give it that
            for sr in releaseConfig['sourceRepositories'].values():
                clone_repositories.update({sr['clonePath']: {}})
            # get_l10n_repositories spits out more than just the repoPath
            # It's easier to just pass it along rather than strip it out
            if len(releaseConfig['l10nPlatforms']) > 0:
                l10n_clone_repos = get_l10n_repositories(
                    releaseConfig['l10nRevisionFile'],
                    releaseConfig['l10nRepoClonePath'],
                    sourceRepoInfo['relbranch'])
                clone_repositories.update(l10n_clone_repos)

            repository_setup_factory = StagingRepositorySetupFactory(
                hgHost=branchConfig['hghost'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                username=releaseConfig['hgUsername'],
                sshKey=releaseConfig['hgSshKey'],
                repositories=clone_repositories,
                clobberURL=branchConfig['base_clobber_url'],
                userRepoRoot=releaseConfig['userRepoRoot'],
            )

            builders.append({
                'name': builderPrefix('repo_setup'),
                'slavenames': unix_slaves,
                'category': builderPrefix(''),
                'builddir': builderPrefix('repo_setup'),
                'slavebuilddir': reallyShort(builderPrefix('repo_setup')),
                'factory': repository_setup_factory,
                'env': builder_env,
                'properties': { 'slavebuilddir':
                    reallyShort(builderPrefix('repo_setup'))},
            })
        else:
            builders.append(makeDummyBuilder(
                name=builderPrefix('repo_setup'),
                slaves=all_slaves,
                category=builderPrefix(''),
                ))

        if not releaseConfig.get('skip_release_download'):
            release_downloader_factory = ScriptFactory(
                scriptRepo=tools_repo,
                extra_args=[branchConfigFile],
                scriptName='scripts/staging/release_downloader.sh',
            )

            builders.append({
                'name': builderPrefix('release_downloader'),
                'slavenames': unix_slaves,
                'category': builderPrefix(''),
                'builddir': builderPrefix('release_downloader'),
                'slavebuilddir': reallyShort(builderPrefix('release_downloader')),
                'factory': release_downloader_factory,
                'env': builder_env,
                'properties': {'builddir': builderPrefix('release_downloader'),
                               'slavebuilddir': reallyShort(builderPrefix('release_downloader'))}
            })
        else:
            builders.append(makeDummyBuilder(
                name=builderPrefix('release_downloader'),
                slaves=all_slaves,
                category=builderPrefix(''),
                ))

    if not releaseConfig.get('skip_tag'):
        pf = branchConfig['platforms']['linux']
        tag_env = builder_env.copy()
        if pf['env'].get('HG_SHARE_BASE_DIR', None):
            tag_env['HG_SHARE_BASE_DIR'] = pf['env']['HG_SHARE_BASE_DIR']

        tag_factory = ScriptFactory(
            scriptRepo=tools_repo,
            scriptName='scripts/release/tagging.sh',
        )

        builders.append({
            'name': builderPrefix('tag'),
            'slavenames': pf['slaves'],
            'category': builderPrefix(''),
            'builddir': builderPrefix('tag'),
            'slavebuilddir': reallyShort(builderPrefix('tag')),
            'factory': tag_factory,
            'nextSlave': _nextFastReservedSlave,
            'env': tag_env,
            'properties': {'builddir': builderPrefix('tag'), 'slavebuilddir': reallyShort(builderPrefix('tag'))}
        })
        notify_builders.append(builderPrefix('tag'))
    else:
        builders.append(makeDummyBuilder(
            name=builderPrefix('tag'),
            slaves=all_slaves,
            category=builderPrefix(''),
            ))

    if not releaseConfig.get('skip_source'):
        pf = branchConfig['platforms']['linux']
        mozconfig = 'linux/%s/release' % sourceRepoInfo['name']
        source_factory = SingleSourceFactory(
            env=pf['env'],
            objdir=pf['platform_objdir'],
            hgHost=branchConfig['hghost'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            repoPath=sourceRepoInfo['path'],
            productName=releaseConfig['productName'],
            version=releaseConfig['version'],
            baseTag=releaseConfig['baseTag'],
            stagingServer=branchConfig['stage_server'],
            stageUsername=branchConfig['stage_username'],
            stageSshKey=branchConfig['stage_ssh_key'],
            buildNumber=releaseConfig['buildNumber'],
            autoconfDirs=['.', 'js/src'],
            clobberURL=branchConfig['base_clobber_url'],
            mozconfig=mozconfig,
            configRepoPath=branchConfig['config_repo_path'],
            configSubDir=branchConfig['config_subdir'],
        )

        builders.append({
           'name': builderPrefix('source'),
           'slavenames': branchConfig['platforms']['linux']['slaves'],
           'category': builderPrefix(''),
           'builddir': builderPrefix('source'),
           'slavebuilddir': reallyShort(builderPrefix('source')),
           'factory': source_factory,
           'env': builder_env,
           'nextSlave': _nextFastReservedSlave,
           'properties': { 'slavebuilddir':
               reallyShort(builderPrefix('source'))}
        })

        if releaseConfig['xulrunnerPlatforms']:
            mozconfig = 'linux/%s/xulrunner' % sourceRepoInfo['name']
            xulrunner_source_factory = SingleSourceFactory(
                env=pf['env'],
                objdir=pf['platform_objdir'],
                hgHost=branchConfig['hghost'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                repoPath=sourceRepoInfo['path'],
                productName='xulrunner',
                version=releaseConfig['milestone'],
                baseTag=releaseConfig['baseTag'],
                stagingServer=branchConfig['stage_server'],
                stageUsername=branchConfig['stage_username_xulrunner'],
                stageSshKey=branchConfig['stage_ssh_xulrunner_key'],
                buildNumber=releaseConfig['buildNumber'],
                autoconfDirs=['.', 'js/src'],
                clobberURL=branchConfig['base_clobber_url'],
                mozconfig=mozconfig,
                configRepoPath=branchConfig['config_repo_path'],
                configSubDir=branchConfig['config_subdir'],
            )

            builders.append({
               'name': builderPrefix('xulrunner_source'),
               'slavenames': branchConfig['platforms']['linux']['slaves'],
               'category': builderPrefix(''),
               'builddir': builderPrefix('xulrunner_source'),
               'slavebuilddir': reallyShort(builderPrefix('xulrunner_source')),
               'factory': xulrunner_source_factory,
               'env': builder_env,
               'properties': { 'slavebuilddir':
                   reallyShort(builderPrefix('xulrunner_source'))}
            })
    else:
        builders.append(makeDummyBuilder(
            name=builderPrefix('source'),
            slaves=all_slaves,
            category=builderPrefix(''),
            ))
        if releaseConfig['xulrunnerPlatforms']:
            builders.append(makeDummyBuilder(
                name=builderPrefix('xulrunner_source'),
                slaves=all_slaves,
                category=builderPrefix(''),
                ))

    for platform in releaseConfig['enUSPlatforms']:
        # shorthand
        pf = branchConfig['platforms'][platform]
        mozconfig = '%s/%s/release' % (platform, sourceRepoInfo['name'])
        if platform in releaseConfig['talosTestPlatforms']:
            talosMasters = branchConfig['talos_masters']
        else:
            talosMasters = None

        if platform in releaseConfig['unittestPlatforms']:
            packageTests = True
            unittestMasters = branchConfig['unittest_masters']
            unittestBranch = builderPrefix('%s-opt-unittest' % platform)
        else:
            packageTests = False
            unittestMasters = None
            unittestBranch = None

        if not releaseConfig.get('skip_build'):
            build_factory = ReleaseBuildFactory(
                env=pf['env'],
                objdir=pf['platform_objdir'],
                platform=platform,
                hgHost=branchConfig['hghost'],
                repoPath=sourceRepoInfo['path'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                configRepoPath=branchConfig['config_repo_path'],
                configSubDir=branchConfig['config_subdir'],
                profiledBuild=pf['profiled_build'],
                mozconfig=mozconfig,
                buildRevision=releaseTag,
                stageServer=branchConfig['stage_server'],
                stageUsername=branchConfig['stage_username'],
                stageGroup=branchConfig['stage_group'],
                stageSshKey=branchConfig['stage_ssh_key'],
                stageBasePath=branchConfig['stage_base_path'],
                codesighs=False,
                uploadPackages=True,
                uploadSymbols=True,
                createSnippet=False,
                doCleanup=True, # this will clean-up the mac build dirs, but not delete
                                # the entire thing
                buildSpace=10,
                productName=releaseConfig['productName'],
                version=releaseConfig['version'],
                buildNumber=releaseConfig['buildNumber'],
                talosMasters=talosMasters,
                packageTests=packageTests,
                unittestMasters=unittestMasters,
                unittestBranch=unittestBranch,
                clobberURL=branchConfig['base_clobber_url'],
                triggerBuilds=True,
                triggeredSchedulers=[builderPrefix('%s_repack' % platform)],
            )

            builders.append({
                'name': builderPrefix('%s_build' % platform),
                'slavenames': pf['slaves'],
                'category': builderPrefix(''),
                'builddir': builderPrefix('%s_build' % platform),
                'slavebuilddir': reallyShort(builderPrefix('%s_build' % platform)),
                'factory': build_factory,
                'nextSlave': _nextFastReservedSlave,
                'env': builder_env,
                'properties': { 'slavebuilddir':
                    reallyShort(builderPrefix('%s_build' % platform))}
            })
        else:
            builders.append(makeDummyBuilder(
                name=builderPrefix('%s_build' % platform),
                slaves=all_slaves,
                category=builderPrefix(''),
                ))

        if platform in releaseConfig['l10nPlatforms']:
            standalone_factory = ScriptFactory(
                scriptRepo=tools_repo,
                interpreter='bash',
                scriptName='scripts/l10n/standalone_repacks.sh',
                extra_args=[platform, branchConfigFile]
            )
            env = builder_env.copy()
            env.update(pf['env'])
            builders.append({
                'name': builderPrefix("standalone_repack", platform),
                'slavenames': branchConfig['l10n_slaves'][platform],
                'category': builderPrefix(''),
                'builddir': builderPrefix("standalone_repack", platform),
                'factory': standalone_factory,
                'nextSlave': _nextFastReservedSlave,
                'env': env,
                'properties': {'builddir':
                    builderPrefix("standalone_repack", platform)}
            })

            for n, builderName in l10nBuilders(platform).iteritems():
                repack_factory = ScriptFactory(
                    scriptRepo=tools_repo,
                    interpreter='bash',
                    scriptName='scripts/l10n/release_repacks.sh',
                    extra_args=[platform, branchConfigFile,
                                str(l10nChunks), str(n)]
                )
                builddir = builderPrefix('%s_repack' % platform) + \
                                         '_' + str(n)
                env = builder_env.copy()
                env.update(pf['env'])
                builders.append({
                    'name': builderName,
                    'slavenames': branchConfig['l10n_slaves'][platform],
                    'category': builderPrefix(''),
                    'builddir': builddir,
                    'slavebuilddir': reallyShort(builddir),
                    'factory': repack_factory,
                    'nextSlave': _nextFastReservedSlave,
                    'env': env,
                    'properties': {'builddir': builddir, 'slavebuilddir': reallyShort(builddir)}
                })

            builders.append(makeDummyBuilder(
                name=builderPrefix('repack_complete', platform),
                slaves=all_slaves,
                category=builderPrefix(''),
            ))

        if platform in releaseConfig['unittestPlatforms']:
            mochitestLeakThreshold = pf.get('mochitest_leak_threshold', None)
            crashtestLeakThreshold = pf.get('crashtest_leak_threshold', None)
            for suites_name, suites in branchConfig['unittest_suites']:
                # Release builds on mac don't have a11y enabled, do disable the mochitest-a11y test
                if platform.startswith('macosx') and 'mochitest-a11y' in suites:
                    suites = suites[:]
                    suites.remove('mochitest-a11y')

                test_builders.extend(generateTestBuilder(
                    branchConfig, 'release', platform, builderPrefix("%s_test" % platform),
                    builderPrefix("%s-opt-unittest" % platform),
                    suites_name, suites, mochitestLeakThreshold,
                    crashtestLeakThreshold, category=builderPrefix('')))

    for platform in releaseConfig['xulrunnerPlatforms']:
        pf = branchConfig['platforms'][platform]
        xr_env = pf['env'].copy()
        xr_env['SYMBOL_SERVER_USER'] = branchConfig['stage_username_xulrunner']
        xr_env['SYMBOL_SERVER_PATH'] = branchConfig['symbol_server_xulrunner_path']
        xr_env['SYMBOL_SERVER_SSH_KEY'] = \
            xr_env['SYMBOL_SERVER_SSH_KEY'].replace(branchConfig['stage_ssh_key'],
                                                    branchConfig['stage_ssh_xulrunner_key'])
        if not releaseConfig.get('skip_build'):
            xulrunner_build_factory = XulrunnerReleaseBuildFactory(
                env=xr_env,
                objdir=pf['platform_objdir'],
                platform=platform,
                hgHost=branchConfig['hghost'],
                repoPath=sourceRepoInfo['path'],
                buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                configRepoPath=branchConfig['config_repo_path'],
                configSubDir=branchConfig['config_subdir'],
                profiledBuild=None,
                mozconfig = '%s/%s/xulrunner' % (platform, sourceRepoInfo['name']),
                buildRevision=releaseTag,
                stageServer=branchConfig['stage_server'],
                stageUsername=branchConfig['stage_username_xulrunner'],
                stageGroup=branchConfig['stage_group'],
                stageSshKey=branchConfig['stage_ssh_xulrunner_key'],
                stageBasePath=branchConfig['stage_base_path_xulrunner'],
                codesighs=False,
                uploadPackages=True,
                uploadSymbols=True,
                createSnippet=False,
                doCleanup=True, # this will clean-up the mac build dirs, but not delete
                                # the entire thing
                buildSpace=pf.get('build_space', branchConfig['default_build_space']),
                productName='xulrunner',
                version=releaseConfig['milestone'],
                buildNumber=releaseConfig['buildNumber'],
                clobberURL=branchConfig['base_clobber_url'],
                packageSDK=True,
            )
            builders.append({
                'name': builderPrefix('xulrunner_%s_build' % platform),
                'slavenames': pf['slaves'],
                'category': builderPrefix(''),
                'builddir': builderPrefix('xulrunner_%s_build' % platform),
                'slavebuilddir': reallyShort(builderPrefix('xulrunner_%s_build' % platform)),
                'factory': xulrunner_build_factory,
                'env': builder_env,
                'properties': {'slavebuilddir':
                    reallyShort(builderPrefix('xulrunner_%s_build' % platform))}
            })
        else:
            builders.append(makeDummyBuilder(
                name=builderPrefix('xulrunner_%s_build' % platform),
                slaves=all_slaves,
                category=builderPrefix(''),
                ))

    if releaseConfig['doPartnerRepacks']:
         for platform in releaseConfig['l10nPlatforms']:
             partner_repack_factory = PartnerRepackFactory(
                 hgHost=branchConfig['hghost'],
                 repoPath=sourceRepoInfo['path'],
                 buildToolsRepoPath=branchConfig['build_tools_repo_path'],
                 productName=releaseConfig['productName'],
                 version=releaseConfig['version'],
                 buildNumber=releaseConfig['buildNumber'],
                 partnersRepoPath=releaseConfig['partnersRepoPath'],
                 partnersRepoRevision=releaseTag,
                 platformList=[platform],
                 stagingServer=releaseConfig['stagingServer'],
                 stageUsername=branchConfig['stage_username'],
                 stageSshKey=branchConfig['stage_ssh_key'],
             )
  
             if 'macosx64' in branchConfig['platforms']:
                 slaves = branchConfig['platforms']['macosx64']['slaves']
             else:
                 slaves = branchConfig['platforms']['macosx']['slaves']
             builders.append({
                 'name': builderPrefix('partner_repack', platform),
                 'slavenames': slaves,
                 'category': builderPrefix(''),
                 'builddir': builderPrefix('partner_repack', platform),
                 'slavebuilddir': reallyShort(builderPrefix('partner_repack', platform)),
                 'factory': partner_repack_factory,
                 'nextSlave': _nextFastReservedSlave,
                 'env': builder_env,
                 'properties': {'slavebuilddir':
                     reallyShort(builderPrefix('partner_repack', platform))}
             })

    for platform in releaseConfig['l10nPlatforms']:
        l10n_verification_factory = L10nVerifyFactory(
            hgHost=branchConfig['hghost'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            cvsroot=releaseConfig['cvsroot'],
            stagingServer=releaseConfig['stagingServer'],
            productName=releaseConfig['productName'],
            version=releaseConfig['version'],
            buildNumber=releaseConfig['buildNumber'],
            oldVersion=releaseConfig['oldVersion'],
            oldBuildNumber=releaseConfig['oldBuildNumber'],
            clobberURL=branchConfig['base_clobber_url'],
            platform=platform,
        )

        if 'macosx64' in branchConfig['platforms']:
            slaves = branchConfig['platforms']['macosx64']['slaves']
        else:
            slaves = branchConfig['platforms']['macosx']['slaves']
        builders.append({
            'name': builderPrefix('l10n_verification', platform),
            'slavenames': slaves,
            'category': builderPrefix(''),
            'builddir': builderPrefix('l10n_verification', platform),
            'slavebuilddir': reallyShort(builderPrefix('l10n_verification', platform)),
            'factory': l10n_verification_factory,
            'nextSlave': _nextFastReservedSlave,
            'env': builder_env,
            'properties': {'slavebuilddir':
                reallyShort(builderPrefix('l10n_verification', platform))}
        })

    if not releaseConfig.get('skip_updates'):
        updates_factory = ReleaseUpdatesFactory(
            hgHost=branchConfig['hghost'],
            repoPath=sourceRepoInfo['path'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            cvsroot=releaseConfig['cvsroot'],
            patcherToolsTag=releaseConfig['patcherToolsTag'],
            patcherConfig=releaseConfig['patcherConfig'],
            verifyConfigs=releaseConfig['verifyConfigs'],
            appName=releaseConfig['appName'],
            productName=releaseConfig['productName'],
            version=releaseConfig['version'],
            appVersion=releaseConfig['appVersion'],
            baseTag=releaseConfig['baseTag'],
            buildNumber=releaseConfig['buildNumber'],
            oldVersion=releaseConfig['oldVersion'],
            oldAppVersion=releaseConfig['oldAppVersion'],
            oldBaseTag=releaseConfig['oldBaseTag'],
            oldBuildNumber=releaseConfig['oldBuildNumber'],
            ftpServer=releaseConfig['ftpServer'],
            bouncerServer=releaseConfig['bouncerServer'],
            stagingServer=releaseConfig['stagingServer'],
            useBetaChannel=releaseConfig['useBetaChannel'],
            stageUsername=branchConfig['stage_username'],
            stageSshKey=branchConfig['stage_ssh_key'],
            ausUser=releaseConfig['ausUser'],
            ausSshKey=releaseConfig['ausSshKey'],
            ausHost=branchConfig['aus2_host'],
            ausServerUrl=releaseConfig['ausServerUrl'],
            hgSshKey=releaseConfig['hgSshKey'],
            hgUsername=releaseConfig['hgUsername'],
            # We disable this on staging, because we don't have a CVS mirror to
            # commit to
            commitPatcherConfig=releaseConfig['commitPatcherConfig'],
            clobberURL=branchConfig['base_clobber_url'],
            oldRepoPath=sourceRepoInfo['path'],
            releaseNotesUrl=releaseConfig['releaseNotesUrl'],
            binaryName=releaseConfig['binaryName'],
            oldBinaryName=releaseConfig['oldBinaryName'],
            testOlderPartials=releaseConfig['testOlderPartials'],
        )

        builders.append({
            'name': builderPrefix('updates'),
            'slavenames': branchConfig['platforms']['linux']['slaves'],
            'category': builderPrefix(''),
            'builddir': builderPrefix('updates'),
            'slavebuilddir': reallyShort(builderPrefix('updates')),
            'factory': updates_factory,
            'nextSlave': _nextFastReservedSlave,
            'env': builder_env,
            'properties': {'slavebuilddir': reallyShort(builderPrefix('updates'))}
        })
    else:
        builders.append(makeDummyBuilder(
            name=builderPrefix('updates'),
            slaves=all_slaves,
            category=builderPrefix('')
        ))


    for platform in sorted(releaseConfig['verifyConfigs'].keys()):
        for n, builderName in updateVerifyBuilders(platform).iteritems():
            uv_factory = ScriptFactory(
                scriptRepo=tools_repo,
                interpreter='bash',
                scriptName='scripts/release/updates/chunked-verify.sh',
                extra_args=[platform, releaseConfig['verifyConfigs'][platform],
                            str(updateVerifyChunks), str(n)],
                log_eval_func=lambda c, s: regex_log_evaluator(c, s, update_verify_error)
            )

            builddir = builderPrefix('%s_update_verify' % platform) + \
                                     '_' + str(n)
            env = builder_env.copy()
            env.update(branchConfig['platforms'][platform]['env'])

            builders.append({
                'name': builderName,
                'slavenames': branchConfig['platforms'][platform]['slaves'],
                'category': builderPrefix(''),
                'builddir': builddir,
                'slavebuilddir': reallyShort(builddir),
                'factory': uv_factory,
                'nextSlave': _nextFastReservedSlave,
                'env': env,
                'properties': {'builddir': builddir,
                               'slavebuilddir': reallyShort(builddir),
                               'script_repo_revision': runtimeTag,
                               'release_tag': releaseTag,
                               'release_config': releaseConfigFile},
            })

    check_permissions_factory = ScriptFactory(
        scriptRepo=tools_repo,
        extra_args=[branchConfigFile, 'permissions'],
        script_timeout=3*60*60,
        scriptName='scripts/release/push-to-mirrors.sh',
    )

    builders.append({
        'name': builderPrefix('check_permissions'),
        'slavenames': unix_slaves,
        'category': builderPrefix(''),
        'builddir': builderPrefix('check_permissions'),
        'slavebuilddir': reallyShort(builderPrefix('chk_prms')),
        'factory': check_permissions_factory,
        'env': builder_env,
        'properties': {'slavebuilddir': reallyShort(builderPrefix('chk_prms')),
                       'script_repo_revision': releaseTag,
                       'release_config': releaseConfigFile},
    })

    antivirus_factory = ScriptFactory(
        scriptRepo=tools_repo,
        extra_args=[branchConfigFile, 'antivirus'],
        script_timeout=3*60*60,
        scriptName='scripts/release/push-to-mirrors.sh',
    )

    builders.append({
        'name': builderPrefix('antivirus'),
        'slavenames': unix_slaves,
        'category': builderPrefix(''),
        'builddir': builderPrefix('antivirus'),
        'slavebuilddir': reallyShort(builderPrefix('av')),
        'factory': antivirus_factory,
        'env': builder_env,
        'properties': {'slavebuilddir': reallyShort(builderPrefix('av')),
                       'script_repo_revision': releaseTag,
                       'release_config': releaseConfigFile},
    })

    push_to_mirrors_factory = ScriptFactory(
        scriptRepo=tools_repo,
        extra_args=[branchConfigFile, 'push'],
        script_timeout=3*60*60,
        scriptName='scripts/release/push-to-mirrors.sh',
    )

    push_to_mirrors_factory.addStep(Trigger(
        schedulerNames=[builderPrefix('ready-for-rel-test'),
                        builderPrefix('ready-for-release')],
        copy_properties=['script_repo_revision', 'release_config']
    ))


    builders.append({
        'name': builderPrefix('push_to_mirrors'),
        'slavenames': unix_slaves,
        'category': builderPrefix(''),
        'builddir': builderPrefix('push_to_mirrors'),
        'slavebuilddir': reallyShort(builderPrefix('psh_mrrrs')),
        'factory': push_to_mirrors_factory,
        'env': builder_env,
        'properties': {'slavebuilddir':
                        reallyShort(builderPrefix('psh_mrrrs'))},
    })
    notify_builders.append(builderPrefix('push_to_mirrors'))

    for platform in releaseConfig['verifyConfigs'].keys():
        final_verification_factory = ReleaseFinalVerification(
            hgHost=branchConfig['hghost'],
            platforms=[platform],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            verifyConfigs=releaseConfig['verifyConfigs'],
            clobberURL=branchConfig['base_clobber_url'],
        )

        builders.append({
            'name': builderPrefix('final_verification', platform),
            'slavenames': branchConfig['platforms']['linux']['slaves'],
            'category': builderPrefix(''),
            'builddir': builderPrefix('final_verification', platform),
            'slavebuilddir': reallyShort(builderPrefix('fnl_verf', platform)),
            'factory': final_verification_factory,
            'nextSlave': _nextFastReservedSlave,
            'env': builder_env,
            'properties': {'slavebuilddir':
                           reallyShort(builderPrefix('fnl_verf', platform))}
        })

    builders.append(makeDummyBuilder(
        name=builderPrefix('ready_for_releasetest_testing'),
        slaves=all_slaves,
        category=builderPrefix(''),
        ))
    notify_builders.append(builderPrefix('ready_for_releasetest_testing'))

    builders.append(makeDummyBuilder(
        name=builderPrefix('ready_for_release'),
        slaves=all_slaves,
        category=builderPrefix(''),
        ))
    notify_builders.append(builderPrefix('ready_for_release'))

    if releaseConfig['majorUpdateRepoPath']:
        # Not attached to any Scheduler
        major_update_factory = MajorUpdateFactory(
            hgHost=branchConfig['hghost'],
            repoPath=releaseConfig['majorUpdateRepoPath'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            cvsroot=releaseConfig['cvsroot'],
            patcherToolsTag=releaseConfig['majorPatcherToolsTag'],
            patcherConfig=releaseConfig['majorUpdatePatcherConfig'],
            verifyConfigs=releaseConfig['majorUpdateVerifyConfigs'],
            appName=releaseConfig['appName'],
            productName=releaseConfig['productName'],
            version=releaseConfig['majorUpdateToVersion'],
            appVersion=releaseConfig['majorUpdateAppVersion'],
            baseTag=releaseConfig['majorUpdateBaseTag'],
            buildNumber=releaseConfig['majorUpdateBuildNumber'],
            oldVersion=releaseConfig['version'],
            oldAppVersion=releaseConfig['appVersion'],
            oldBaseTag=releaseConfig['baseTag'],
            oldBuildNumber=releaseConfig['buildNumber'],
            ftpServer=releaseConfig['ftpServer'],
            bouncerServer=releaseConfig['bouncerServer'],
            stagingServer=releaseConfig['stagingServer'],
            useBetaChannel=releaseConfig['useBetaChannel'],
            stageUsername=branchConfig['stage_username'],
            stageSshKey=branchConfig['stage_ssh_key'],
            ausUser=releaseConfig['ausUser'],
            ausSshKey=releaseConfig['ausSshKey'],
            ausHost=branchConfig['aus2_host'],
            ausServerUrl=releaseConfig['ausServerUrl'],
            hgSshKey=releaseConfig['hgSshKey'],
            hgUsername=releaseConfig['hgUsername'],
            # We disable this on staging, because we don't have a CVS mirror to
            # commit to
            commitPatcherConfig=releaseConfig['commitPatcherConfig'],
            clobberURL=branchConfig['base_clobber_url'],
            oldRepoPath=sourceRepoInfo['path'],
            triggerSchedulers=[builderPrefix('major_update_verify')],
            releaseNotesUrl=releaseConfig['majorUpdateReleaseNotesUrl'],
            fakeMacInfoTxt=releaseConfig['majorFakeMacInfoTxt']
        )

        builders.append({
            'name': builderPrefix('major_update'),
            'slavenames': branchConfig['platforms']['linux']['slaves'],
            'category': builderPrefix(''),
            'builddir': builderPrefix('major_update'),
            'slavebuilddir': reallyShort(builderPrefix('mu')),
            'factory': major_update_factory,
            'nextSlave': _nextFastReservedSlave,
            'env': builder_env,
            'properties': {'slavebuilddir': reallyShort(builderPrefix('mu'))}
        })
        notify_builders.append(builderPrefix('major_update'))

        for platform in sorted(releaseConfig['majorUpdateVerifyConfigs'].keys()):
            for n, builderName in majorUpdateVerifyBuilders(platform).iteritems():
                muv_factory = ScriptFactory(
                    scriptRepo=tools_repo,
                    interpreter='bash',
                    scriptName='scripts/release/updates/chunked-verify.sh',
                    extra_args=[platform, releaseConfig['majorUpdateVerifyConfigs'][platform],
                                str(updateVerifyChunks), str(n)],
                    log_eval_func=lambda c, s: regex_log_evaluator(c, s, update_verify_error)
                )

                builddir = builderPrefix('%s_major_update_verify' % platform) + \
                                        '_' + str(n)
                env = builder_env.copy()
                env.update(branchConfig['platforms'][platform]['env'])

                builders.append({
                    'name': builderName,
                    'slavenames': branchConfig['platforms'][platform]['slaves'],
                    'category': builderPrefix(''),
                    'builddir': builddir,
                    'slavebuilddir': reallyShort(builddir),
                    'factory': muv_factory,
                    'nextSlave': _nextFastReservedSlave,
                    'env': env,
                    'properties': {'builddir': builddir,
                                   'slavebuilddir': reallyShort(builddir),
                                   'script_repo_revision': runtimeTag,
                                   'release_tag': releaseTag,
                                   'release_config': releaseConfigFile},
                })

    bouncer_submitter_factory = TuxedoEntrySubmitterFactory(
        baseTag=releaseConfig['baseTag'],
        appName=releaseConfig['appName'],
        config=releaseConfig['tuxedoConfig'],
        productName=releaseConfig['productName'],
        version=releaseConfig['version'],
        milestone=releaseConfig['milestone'],
        tuxedoServerUrl=releaseConfig['tuxedoServerUrl'],
        enUSPlatforms=releaseConfig['enUSPlatforms'],
        l10nPlatforms=releaseConfig['l10nPlatforms'],
        extraPlatforms=releaseConfig.get('extraBouncerPlatforms'),
        oldVersion=releaseConfig['oldVersion'],
        hgHost=branchConfig['hghost'],
        repoPath=sourceRepoInfo['path'],
        buildToolsRepoPath=branchConfig['build_tools_repo_path'],
        credentialsFile=os.path.join(os.getcwd(), "BuildSlaves.py")
    )

    builders.append({
        'name': builderPrefix('bouncer_submitter'),
        'slavenames': branchConfig['platforms']['linux']['slaves'],
        'category': builderPrefix(''),
        'builddir': builderPrefix('bouncer_submitter'),
        'slavebuilddir': reallyShort(builderPrefix('bncr_sub')),
        'factory': bouncer_submitter_factory,
        'env': builder_env,
        'properties': {'slavebuilddir':
            reallyShort(builderPrefix('bncr_sub'))}
    })

    if releaseConfig['doPartnerRepacks']:
        euballot_bouncer_submitter_factory = TuxedoEntrySubmitterFactory(
            baseTag=releaseConfig['baseTag'],
            appName=releaseConfig['appName'],
            config=releaseConfig['tuxedoConfig'],
            productName=releaseConfig['productName'],
            bouncerProductSuffix='EUballot',
            version=releaseConfig['version'],
            milestone=releaseConfig['milestone'],
            tuxedoServerUrl=releaseConfig['tuxedoServerUrl'],
            enUSPlatforms=('win32-EUballot',),
            l10nPlatforms=None, # not needed
            oldVersion=None, # no updates
            hgHost=branchConfig['hghost'],
            repoPath=sourceRepoInfo['path'],
            buildToolsRepoPath=branchConfig['build_tools_repo_path'],
            credentialsFile=os.path.join(os.getcwd(), "BuildSlaves.py"),
        )

        builders.append({
            'name': builderPrefix('euballot_bouncer_submitter'),
            'slavenames': branchConfig['platforms']['linux']['slaves'],
            'category': builderPrefix(''),
            'builddir': builderPrefix('euballot_bouncer_submitter'),
            'slavebuilddir': reallyShort(builderPrefix('eu_bncr_sub')),
            'factory': euballot_bouncer_submitter_factory,
            'env': builder_env,
            'properties': {'slavebuilddir':
                reallyShort(builderPrefix('eu_bncr_sub'))}
        })

    # Separate email messages per list. Mailman doesn't try to avoid duplicate
    # messages in this case. See Bug 635527 for the details.
    tagging_started_recipients = releaseConfig['AllRecipients'][:]
    if not releaseConfig.get('skip_tag'):
        tagging_started_recipients.extend(releaseConfig['PassRecipients'])
    for recipient in tagging_started_recipients:
        #send a message when we receive the sendchange and start tagging
        status.append(ChangeNotifier(
                fromaddr="release@mozilla.com",
                relayhost="mail.build.mozilla.org",
                sendToInterestedUsers=False,
                extraRecipients=[recipient],
                branches=[sourceRepoInfo['path']],
                messageFormatter=createReleaseChangeMessage,
            ))
    for recipient in releaseConfig['AllRecipients'] + \
                     releaseConfig['PassRecipients']:
        #send a message when signing is complete
        status.append(ChangeNotifier(
                fromaddr="release@mozilla.com",
                relayhost="mail.build.mozilla.org",
                sendToInterestedUsers=False,
                extraRecipients=[recipient],
                branches=[builderPrefix('post_signing')],
                messageFormatter=createReleaseChangeMessage,
            ))

    #send the nice(passing) release messages
    status.append(MailNotifier(
            fromaddr='release@mozilla.com',
            sendToInterestedUsers=False,
            extraRecipients=releaseConfig['PassRecipients'],
            mode='passing',
            builders=notify_builders,
            relayhost='mail.build.mozilla.org',
            messageFormatter=createReleaseMessage,
        ))

    #send all release messages
    status.append(MailNotifier(
            fromaddr='release@mozilla.com',
            sendToInterestedUsers=False,
            extraRecipients=releaseConfig['AllRecipients'],
            mode='all',
            categories=[builderPrefix('')],
            relayhost='mail.build.mozilla.org',
            messageFormatter=createReleaseMessage,
        ))

    status.append(MailNotifier(
            fromaddr='release@mozilla.com',
            sendToInterestedUsers=False,
            extraRecipients=releaseConfig['AVVendorsRecipients'],
            mode='passing',
            builders=[builderPrefix('updates')],
            relayhost='mail.build.mozilla.org',
            messageFormatter=createReleaseAVVendorsMessage,
        ))

    status.append(TinderboxMailNotifier(
        fromaddr="mozilla2.buildbot@build.mozilla.org",
        tree=branchConfig["tinderbox_tree"] + "-Release",
        extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org",],
        relayhost="mail.build.mozilla.org",
        builders=[b['name'] for b in builders],
        logCompression="gzip")
    )

    status.append(TinderboxMailNotifier(
        fromaddr="mozilla2.buildbot@build.mozilla.org",
        tree=branchConfig["tinderbox_tree"] + "-Release",
        extraRecipients=["tinderbox-daemon@tinderbox.mozilla.org",],
        relayhost="mail.build.mozilla.org",
        builders=[b['name'] for b in test_builders],
        logCompression="gzip",
        errorparser="unittest")
    )

    builders.extend(test_builders)

    logUploadCmd = makeLogUploadCommand(sourceRepoInfo['name'], branchConfig,
            platform_prop=None)

    status.append(SubprocessLogHandler(
        logUploadCmd + [
            '--release', '%s/%s' % (
                releaseConfig['version'], releaseConfig['buildNumber'])
            ],
        builders=[b['name'] for b in builders + test_builders],
    ))

    return {
            "builders": builders,
            "status": status,
            "change_source": change_source,
            "schedulers": schedulers,
            }
