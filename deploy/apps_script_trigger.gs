/**
 * Starts the clip pipeline's GitHub workflows on time, from Google's servers.
 *
 * Why: GitHub starts scheduled workflows "best effort". On this repository its
 * timer has skipped whole mornings and once started the daily run nine hours
 * late. Google's time triggers fire within about fifteen minutes of the time
 * asked for, every day, with no computer of yours switched on.
 *
 * What it does: nothing but press "Run workflow" through GitHub's official
 * API. The workflows protect themselves: a second start of a day that already
 * finished, or a round outside the dates in the settings, ends in seconds.
 *
 * Set up once (about five minutes), at https://script.google.com :
 *   1. New project, paste this file, save.
 *   2. Project Settings > Script Properties, add two properties:
 *        REPO          owner/name of the public repository, e.g. yourname/stream-clipper
 *        GITHUB_TOKEN  a fine-grained personal access token from
 *                      GitHub > Settings > Developer settings > Fine-grained tokens:
 *                      "Only select repositories" = that one repository,
 *                      Repository permissions > Actions = Read and write. Nothing else.
 *   3. Run the function `test` once and approve the permission prompt. It starts
 *      the post check on GitHub; "ok" in the log means the token works.
 *   4. Run the function `install` once. It creates the timers listed in PLAN.
 *
 * To stop it: run `uninstall`, or delete the token on GitHub.
 * The token is the only secret and lives in Script Properties, never in this file.
 */

const ZONE = 'Africa/Cairo';

// [function to call, hour, minute] in Cairo time. Google fires within ~15 minutes.
const PLAN = [
  ['startDaily', 9, 30],    // the day's run
  ['startDaily', 11, 0],    // second chance: ends in seconds if the first one finished
  ['startRound', 15, 45],   // round for the 6:00 PM slot (only on the dates in the settings)
  ['startRound', 19, 45],   // round for the 10:00 PM slot
  ['startCheck', 18, 25],   // did the 6:00 PM post go out?
  ['startCheck', 19, 10],
  ['startCheck', 22, 25],   // the 10:00 PM post
  ['startCheck', 23, 10],
  ['startCheck', 3, 25],    // the 3:00 AM post
  ['startCheck', 4, 10],
];

function startDaily() {
  dispatch_('daily.yml', {dry_run: 'false', force: 'false', state_branch: 'main'});
}

function startRound() {
  dispatch_('daily.yml', {round: 'true', dry_run: 'false', force: 'false', state_branch: 'main'});
}

function startCheck() {
  dispatch_('check.yml', null);
}

/** Run once by hand: proves the repository name and the token are right. */
function test() {
  startCheck();
  console.log('ok: GitHub accepted the request. A "check" run should appear under Actions within a minute.');
}

/** Run once by hand: replaces this project's timers with the ones in PLAN. */
function install() {
  uninstall();
  PLAN.forEach(function (row) {
    ScriptApp.newTrigger(row[0]).timeBased().atHour(row[1]).nearMinute(row[2]).everyDays(1)
        .inTimezone(ZONE).create();
  });
  console.log('installed ' + PLAN.length + ' timers');
}

function uninstall() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
}

function dispatch_(workflow, inputs) {
  const props = PropertiesService.getScriptProperties();
  const repo = (props.getProperty('REPO') || '').trim();
  const token = (props.getProperty('GITHUB_TOKEN') || '').trim();
  if (!repo || !token) {
    throw new Error('Set the script properties REPO and GITHUB_TOKEN first (see the top of this file).');
  }
  const body = {ref: 'main'};
  if (inputs) body.inputs = inputs;
  const res = UrlFetchApp.fetch(
      'https://api.github.com/repos/' + repo + '/actions/workflows/' + workflow + '/dispatches', {
        method: 'post',
        contentType: 'application/json',
        headers: {
          Authorization: 'Bearer ' + token,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
        },
        payload: JSON.stringify(body),
        muteHttpExceptions: true,
      });
  const code = res.getResponseCode();
  if (code !== 204) {
    // Thrown errors reach you through Apps Script's own failure emails.
    // 401: the token expired or was deleted. 404: wrong REPO, or the token cannot see it.
    throw new Error('GitHub refused to start ' + workflow + ' (HTTP ' + code + '): ' +
                    res.getContentText().slice(0, 300));
  }
}
