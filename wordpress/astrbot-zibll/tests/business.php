<?php

if (PHP_SAPI !== 'cli' || !getenv('AZ_WP_ROOT')) {
    exit(1);
}
$root = rtrim(getenv('AZ_WP_ROOT'), '/') . '/';
define('ABSPATH', $root);
define('DONOTCACHEPAGE', true);
define('WP_DISABLE_FATAL_ERROR_HANDLER', true);
require_once ABSPATH . 'wp-includes/plugin.php';
add_filter('enable_loading_advanced_cache_dropin', static fn() => false);
add_filter('pre_http_request', static fn() => new WP_Error('test_network_disabled'));
add_filter('pre_wp_mail', static fn() => false);
add_filter('zib_temp_dir', static fn() => sys_get_temp_dir() . '/az-theme-test');
$_SERVER['HTTP_HOST'] = 'localhost:18797';
$_SERVER['HTTPS'] = 'on';
$_SERVER['REQUEST_METHOD'] = 'GET';
$_SERVER['REQUEST_URI'] = '/';

if (($argv[1] ?? '') !== '--run' && ($argv[1] ?? '') !== '--checkin-worker') {
    define('SHORTINIT', true);
    require ABSPATH . 'wp-load.php';
    $prefix = 'azb_' . bin2hex(random_bytes(6)) . '_';
    try {
        foreach ($wpdb->tables('all') as $source) {
            $suffix = substr($source, strlen($wpdb->prefix));
            if (!preg_match('/\A[a-z_]+\z/', $suffix)) {
                throw new RuntimeException('Unsafe source table.');
            }
            $target = $prefix . $suffix;
            if ($wpdb->query("CREATE TABLE $target LIKE $source") === false) {
                throw new RuntimeException('Cannot create fixture schema.');
            }
        }
        $theme_options = array(
            'checkin_s' => true, 'points_s' => true, 'user_level_s' => true, 'user_ban_s' => true,
            'checkin_reward_points' => 10, 'checkin_reward_integral' => 2,
            'points_free_opt' => array('day_max' => 100),
            'user_integral_opt' => array('day_max' => 100),
            'user_level_max' => 10,
        );
        foreach (array(
            'home' => 'https://localhost:18797', 'siteurl' => 'https://localhost:18797',
            'template' => 'zibll', 'stylesheet' => 'zibll', 'timezone_string' => 'Asia/Shanghai',
            'blogname' => 'AstrBot isolated tests', 'blog_public' => '1',
            'active_plugins' => array(), 'zibll_options' => $theme_options,
        ) as $name => $value) {
            $wpdb->insert($prefix . 'options', array(
                'option_name' => $name, 'option_value' => maybe_serialize($value), 'autoload' => 'yes',
            ));
        }
        foreach (array('AZ_DB_NAME' => DB_NAME, 'AZ_DB_HOST' => DB_HOST, 'AZ_DB_USER' => DB_USER, 'AZ_DB_SECRET' => DB_PASSWORD) as $name => $value) {
            putenv($name . '=' . $value);
        }
        $process = proc_open(array(PHP_BINARY, __FILE__, '--run', $prefix), array(0 => STDIN, 1 => STDOUT, 2 => STDERR), $pipes);
        $status = proc_close($process);
    } finally {
        $tables = $wpdb->get_col($wpdb->prepare('SHOW TABLES LIKE %s', $wpdb->esc_like($prefix) . '%'));
        foreach ($tables as $table) {
            if (!preg_match('/\Aazb_[0-9a-f]{12}_[a-z_]+\z/', $table) || !str_starts_with($table, $prefix)) {
                throw new RuntimeException('Unsafe fixture cleanup.');
            }
            $wpdb->query("DROP TABLE `$table`");
        }
        $remaining = $wpdb->get_col($wpdb->prepare('SHOW TABLES LIKE %s', $wpdb->esc_like($prefix) . '%'));
        if ($remaining) {
            throw new RuntimeException('Fixture cleanup failed.');
        }
        echo "Business fixture tables remaining: 0\n";
    }
    exit($status);
}

$table_prefix = $argv[2] ?? '';
if (!preg_match('/\Aazb_[0-9a-f]{12}_\z/', $table_prefix)) {
    throw new RuntimeException('Invalid fixture prefix.');
}
define('DB_NAME', getenv('AZ_DB_NAME'));
define('DB_USER', getenv('AZ_DB_USER'));
define('DB_PASSWORD', getenv('AZ_DB_SECRET'));
define('DB_HOST', getenv('AZ_DB_HOST'));
define('DB_CHARSET', 'utf8mb4');
define('DB_COLLATE', '');
define('WP_CACHE', false);
define('WP_DEBUG', false);
define('DISABLE_WP_CRON', true);
define('WP_AUTO_UPDATE_CORE', false);
define('AUTOMATIC_UPDATER_DISABLED', true);
define('DISALLOW_FILE_MODS', true);
define('WPMU_PLUGIN_DIR', __DIR__ . '/no-mu-plugins');
define('WP_PLUGIN_DIR', __DIR__ . '/no-plugins');
foreach (array('AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY', 'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT') as $constant) {
    define($constant, hash('sha256', 'az-fixture-' . $constant));
}
require ABSPATH . 'wp-settings.php';
require dirname(__DIR__) . '/astrbot-zibll.php';
if (wp_using_ext_object_cache()) {
    throw new RuntimeException('Shared object cache is not permitted.');
}
$checks = 0;
function verify($condition, $label)
{
    global $checks;
    if (!$condition) {
        throw new RuntimeException($label);
    }
    ++$checks;
}

function call_api($route, $actor, $extra = array())
{
    $options = AstrBotZibll\settings();
    $body = wp_json_encode(array_merge(array('actor' => $actor), $extra), JSON_UNESCAPED_UNICODE);
    $request = new WP_REST_Request('POST', AstrBotZibll\NAMESPACE_PATH . '/' . $route);
    $request->set_body($body);
    $request->set_header('content-type', 'application/json');
    $timestamp = (string) time();
    $nonce = bin2hex(random_bytes(16));
    foreach (array(
        'x-az-client' => $options['client_id'], 'x-az-key' => $options['key_id'],
        'x-az-time' => $timestamp, 'x-az-nonce' => $nonce,
        'x-az-signature' => AstrBotZibll\signature($options['client_id'], $options['key_id'], $options['secret'], 'POST', $request->get_route(), $timestamp, $nonce, $body),
    ) as $name => $value) {
        $request->set_header($name, $value);
    }
    $server = rest_get_server();
    return apply_filters('rest_post_dispatch', $server->dispatch($request), $server, $request);
}

function clear_fixture_limits()
{
    global $wpdb;
    $wpdb->query("DELETE FROM " . AstrBotZibll\state_table() . " WHERE hits > 0");
}

function authorize_actor($actor, $uid)
{
    clear_fixture_limits();
    $start = call_api('binding/start', $actor)->get_data()['data'];
    parse_str(parse_url($start['url'], PHP_URL_QUERY), $query);
    $token = $query['az_bind'];
    wp_set_current_user($uid);
    $authorized = AstrBotZibll\browser_authorize($token, $uid, wp_create_nonce('az_bind_' . $token));
    verify(!is_wp_error($authorized), 'Website login and nonce authorizes fixture');
    $status = call_api('binding/status', $actor)->get_data()['data'];
    return call_api('binding/confirm', $actor, array('expected_uid' => $uid, 'challenge_id' => $status['challenge_id']));
}

if ($argv[1] === '--checkin-worker') {
    while (microtime(true) < (float) $argv[5]) {
        usleep(1000);
    }
    $uid = (int) $argv[3];
    if ($argv[4] === 'web') {
        wp_set_current_user($uid);
        do_action('wp_ajax_user_checkin');
        exit(2);
    }
    $response = AstrBotZibll\checkin($uid, 'parallel-actor', array(
        'request_id' => hash('sha256', $argv[4]), 'event_time' => time(),
    ), false);
    echo is_wp_error($response) ? $response->get_error_code() : $response->get_data()['data']['state'];
    exit;
}

AstrBotZibll\install();
verify(AstrBotZibll\theme_status()['ready'], 'Real Zibll theme loaded');
update_option(AstrBotZibll\OPTION, array(
    'enabled' => true, 'client_id' => 'fixture-client', 'key_id' => 'fixture-key',
    'secret' => bin2hex(random_bytes(32)), 'allow_local_http' => false,
    'allowed_groups' => array('p|bot|g', 'p|bot|other'),
), false);
$GLOBALS['wp_rest_server'] = new WP_REST_Server();
do_action('rest_api_init');
remove_all_actions('user_register');
$uid = wp_insert_user(array('user_login' => 'fixture-one', 'user_pass' => wp_generate_password(), 'display_name' => 'Test Member'));
$uid2 = wp_insert_user(array('user_login' => 'fixture-two', 'user_pass' => wp_generate_password(), 'display_name' => 'Other Member'));
verify(is_int($uid) && is_int($uid2), 'Only isolated fixture users created');
$actor = array('platform_id' => 'p', 'bot_self_id' => 'bot', 'group_id' => 'g', 'sender_id' => '001');
verify(call_api('profile', $actor)->get_data()['code'] === 'not_bound', 'Profile requires binding');
verify(call_api('profile', array_merge($actor, array('group_id' => 'forbidden')))->get_status() === 403, 'Website group allowlist enforced');
$start = call_api('binding/start', $actor)->get_data()['data'];
parse_str(parse_url($start['url'], PHP_URL_QUERY), $query);
$token = $query['az_bind'];
verify(call_api('binding/status', $actor)->get_data()['data']['state'] === 'awaiting_website', 'Link alone cannot bind');
wp_set_current_user($uid);
verify(is_wp_error(AstrBotZibll\browser_authorize($token, $uid, 'bad')), 'Website CSRF rejected');
verify(!is_wp_error(AstrBotZibll\browser_authorize($token, $uid, wp_create_nonce('az_bind_' . $token))), 'Real login nonce accepted');
wp_set_current_user($uid2);
verify(is_wp_error(AstrBotZibll\browser_authorize($token, $uid2, wp_create_nonce('az_bind_' . $token))), 'Candidate cannot be replaced');
clear_fixture_limits();
$status = call_api('binding/status', $actor)->get_data()['data'];
verify($status['uid'] === $uid && $status['state'] === 'awaiting_confirmation', 'Candidate shown only after website authorization');
verify(call_api('binding/confirm', $actor, array('expected_uid' => $uid2, 'challenge_id' => $status['challenge_id']))->get_data()['code'] === 'candidate_mismatch', 'Wrong expected UID rejected');
verify(call_api('binding/confirm', $actor, array('expected_uid' => $uid, 'challenge_id' => $status['challenge_id']))->get_data()['data']['state'] === 'bound', 'Same actor confirms authorized UID');
$different = array_merge($actor, array('sender_id' => '002'));
verify(authorize_actor($different, $uid)->get_data()['code'] === 'binding_conflict', 'Unique website user per scope enforced');
verify(authorize_actor(array_merge($actor, array('group_id' => 'other')), $uid)->get_status() === 200, 'Cross-group separate authorization allowed');
clear_fixture_limits();
$profile = call_api('profile', $actor)->get_data()['data'];
verify(array_keys($profile) === array('uid', 'nickname', 'avatar_url', 'joined_days', 'level', 'points', 'post_count', 'comment_count'), 'Profile contains only eight requested fields');
verify($profile['uid'] === $uid && $profile['level'] === 1 && $profile['points'] === 0, 'Profile uses bound user and theme values');
verify(!metadata_exists('user', $uid, 'level'), 'Profile does not initialize level meta');
verify(call_api('profile', $actor, array('uid' => $uid2))->get_status() === 400, 'Client cannot choose another UID');
update_user_meta($uid, 'banned', 2);
verify(call_api('profile', $actor)->get_data()['code'] === 'account_restricted', 'Restricted account denied');
delete_user_meta($uid, 'banned');

remove_all_actions('save_post');
remove_all_actions('wp_insert_post');
remove_all_actions('wp_after_insert_post');
foreach (array(
    array('Public needle', 'publish', ''),
    array('Private needle', 'private', ''),
    array('Protected needle', 'publish', 'secret'),
    array('Draft needle', 'draft', ''),
    array('Body only', 'publish', ''),
) as [$title, $state, $password]) {
    wp_insert_post(array(
        'post_title' => $title, 'post_status' => $state, 'post_password' => $password,
        'post_content' => 'needle hidden body', 'post_author' => $uid, 'post_type' => 'post',
    ));
}
clear_fixture_limits();
$items = call_api('articles/search', $actor, array('keyword' => 'needle'))->get_data()['data']['items'];
verify(count($items) === 1 && $items[0]['title'] === 'Public needle', 'Search only public unprotected titles, not body: ' . wp_json_encode($items));
verify(array_keys($items[0]) === array('title', 'published_at', 'url'), 'Article response excludes content');
verify(count(call_api('articles/latest', $actor)->get_data()['data']['items']) === 2, 'Latest excludes draft/private/password posts');
verify(call_api('articles/search', $actor, array('keyword' => ''))->get_status() === 400, 'Empty keyword rejected');
$hidden = static fn($visible, $post) => $post->post_title !== 'Public needle';
add_filter('astrbot_zibll_article_visible', $hidden, 10, 2);
verify(call_api('articles/search', $actor, array('keyword' => 'needle'))->get_data()['data']['items'] === array(), 'Site visibility filter can exclude public metadata');
remove_filter('astrbot_zibll_article_visible', $hidden);

clear_fixture_limits();
$event = array('request_id' => hash('sha256', 'event-one'), 'event_time' => time());
$response = call_api('checkin', $actor, $event);
verify($response->get_status() === 200 && $response->get_data()['data']['state'] === 'completed', 'Real theme checkin completes: ' . wp_json_encode(array(
    'response' => $response->get_data(), 'reward' => zib_get_user_the_checkin_details($uid),
    'snapshot' => AstrBotZibll\reward_snapshot($uid),
)));
verify(zibpay_get_user_points($uid) === 10 && zib_get_user_checkin_all_day($uid) === 1, 'Theme points and total days updated once');
verify((int) get_user_meta($uid, 'level_integral', true) === 2, 'Theme experience hook ran');
$record = zib_get_user_meta($uid, 'points_record', true);
verify(count($record) === 1 && $record[0]['value'] === 10, 'Real theme reward ledger recorded once');
verify(call_api('checkin', $actor, $event)->get_data()['data']['state'] === 'completed', 'Same event deduplicated');
verify(call_api('checkin', array_merge($actor, array('group_id' => 'other')), array(
    'request_id' => hash('sha256', 'cross-group'), 'event_time' => time(),
))->get_data()['data']['state'] === 'already_checked_in', 'Second group cannot reward twice');
verify(zibpay_get_user_points($uid) === 10, 'No duplicate points');
verify(call_api('checkin/status', $actor, $event)->get_data()['data']['state'] === 'completed', 'Read-only operation status available');
verify(call_api('checkin', $actor, array_merge($event, array('event_time' => $event['event_time'] - 1)))->get_data()['code'] === 'request_conflict', 'Reused ID with different input rejected');
verify(call_api('checkin', $actor, array('request_id' => hash('sha256', 'old'), 'event_time' => time() - 1000))->get_data()['code'] === 'event_expired', 'Stale events do not cross days');

clear_fixture_limits();
verify(call_api('binding/revoke', $actor, array('confirm' => true))->get_data()['code'] === 'confirmation_expired', 'Unbind requires first confirmation step');
verify(call_api('binding/revoke', $actor)->get_data()['data']['uid'] === $uid, 'Unbind previews current UID');
verify(call_api('binding/revoke', $actor, array('confirm' => true))->get_data()['data']['state'] === 'revoked', 'Actor can revoke own binding');
verify(call_api('profile', $actor)->get_data()['code'] === 'not_bound', 'Revocation takes effect immediately');

$workers = array();
$start = microtime(true) + 3;
for ($i = 0; $i < 8; ++$i) {
    $p = proc_open(array(PHP_BINARY, __FILE__, '--checkin-worker', $table_prefix, (string) $uid2, $i === 0 ? 'web' : (string) $i, (string) $start), array(
        0 => array('pipe', 'r'), 1 => array('pipe', 'w'), 2 => array('pipe', 'w'),
    ), $pipes);
    fclose($pipes[0]);
    $workers[] = array($p, $pipes);
}
$states = array();
$worker_failures = array();
foreach ($workers as [$process, $pipes]) {
    $output = trim(stream_get_contents($pipes[1]));
    $web = json_decode($output, true);
    if (is_array($web)) {
        $states[] = !empty($web['data']['time']) ? 'completed'
            : (str_contains($web['msg'] ?? '', '今日已签到') ? 'already_checked_in' : 'busy');
    } else {
        $states[] = $output;
    }
    $errors = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $exit = proc_close($process);
    if ($exit !== 0 || $errors !== '') {
        $worker_failures[] = array('exit' => $exit, 'output' => $output, 'error' => $errors);
    }
}
verify(!$worker_failures, 'Parallel real-theme worker exits cleanly: ' . wp_json_encode($worker_failures));
verify(count(array_filter($states, static fn($s) => $s === 'completed')) === 1, 'One concurrent checkin executes: ' . wp_json_encode($states));
verify(count(array_filter($states, static fn($s) => in_array($s, array('already_checked_in', 'busy'), true))) === 7, 'Other concurrent calls are idempotent or explicitly busy: ' . wp_json_encode($states));
AstrBotZibll\fresh_user_meta($uid2);
verify(zibpay_get_user_points($uid2) === 10 && zib_get_user_checkin_all_day($uid2) === 1, 'Parallel calls award once');
$ledger = zib_get_user_meta($uid2, 'points_record', true);
verify(count($ledger) === 1, 'Web AJAX and bridge concurrent checkin record one reward');

$uid3 = wp_insert_user(array('user_login' => 'fixture-failure', 'user_pass' => wp_generate_password()));
$throw = static function ($id) use ($uid3) {
    if ($id === $uid3) {
        throw new RuntimeException('Injected reward hook interruption');
    }
};
add_action('user_checkined', $throw, 5);
$failure_event = array('request_id' => hash('sha256', 'interrupted'), 'event_time' => time());
$partial = AstrBotZibll\checkin($uid3, 'failure-actor', $failure_event, false);
remove_action('user_checkined', $throw, 5);
verify($partial->get_data()['data']['state'] === 'uncertain', 'Interrupted reward hook is not reported as success');
verify(zib_get_user_the_checkin_details($uid3) && zibpay_get_user_points($uid3) === 0, 'Partial theme state retained without manual repair');
$retry = AstrBotZibll\checkin($uid3, 'other-failure-actor', array(
    'request_id' => hash('sha256', 'new-attempt'), 'event_time' => time(),
), false);
verify($retry->get_data()['data']['state'] === 'uncertain' && zibpay_get_user_points($uid3) === 0, 'Uncertain daily operation is never replayed');

$uid4 = wp_insert_user(array('user_login' => 'fixture-midnight', 'user_pass' => wp_generate_password()));
$today = current_time('Y-m-d');
$zone = (new DateTimeImmutable('now', new DateTimeZone('Pacific/Kiritimati')))->format('Y-m-d') !== $today
    ? 'Pacific/Kiritimati' : 'Etc/GMT+12';
$flip = static function () use ($zone) {
    update_option('timezone_string', $zone);
};
add_action('user_checkined', $flip, PHP_INT_MAX);
$boundary = AstrBotZibll\checkin($uid4, 'midnight-actor', array(
    'request_id' => hash('sha256', 'midnight'), 'event_time' => time(),
), false);
remove_action('user_checkined', $flip, PHP_INT_MAX);
update_option('timezone_string', 'Asia/Shanghai');
verify($boundary->get_data()['data']['state'] === 'uncertain', 'A site-date change during checkin is never auto-replayed');

$uid5 = wp_insert_user(array('user_login' => 'fixture-capped', 'user_pass' => wp_generate_password()));
zib_update_user_meta($uid5, 'free_points_detail', array(current_time('Y-m-d') => 100));
zib_update_user_meta($uid5, 'level_integral_date_detail', array(current_time('Y-m-d') => 100));
$capped = AstrBotZibll\checkin($uid5, 'capped-actor', array(
    'request_id' => hash('sha256', 'capped'), 'event_time' => time(),
), false);
verify($capped->get_data()['data']['state'] === 'completed', 'Daily reward cap does not falsely fail a valid checkin');
verify(zibpay_get_user_points($uid5) === 0 && (int) get_user_meta($uid5, 'level_integral', true) === 0, 'Bridge does not bypass theme reward caps');

$uid6 = wp_insert_user(array('user_login' => 'fixture-missing-hook', 'user_pass' => wp_generate_password()));
$hooks = clone $GLOBALS['wp_filter']['user_checkined'];
remove_all_actions('user_checkined');
$missing = AstrBotZibll\checkin($uid6, 'missing-hook-actor', array(
    'request_id' => hash('sha256', 'missing-hook'), 'event_time' => time(),
), false);
$GLOBALS['wp_filter']['user_checkined'] = $hooks;
verify($missing->get_data()['data']['state'] === 'uncertain', 'Missing reward listener is not reported as completed');

$request = new WP_REST_Request('POST');
$request->set_header('content-type', 'application/json');
$request->set_body(wp_json_encode(array('actor' => array_merge($actor, array('group_id' => 'other')))));
$hash = AstrBotZibll\actor_data($request)['actor_hash'];
verify(is_wp_error(AstrBotZibll\revoke_binding($hash, $uid2)), 'Website cannot revoke another user binding');
verify(!is_wp_error(AstrBotZibll\revoke_binding($hash, $uid)), 'Website owner can revoke own binding');
clear_fixture_limits();
verify(call_api('profile', array_merge($actor, array('group_id' => 'other')))->get_data()['code'] === 'not_bound', 'Website revocation invalidates next group request');
for ($i = 0; $i < 10; ++$i) {
    call_api('articles/latest', $actor);
}
verify(call_api('articles/latest', $actor)->get_status() === 429, 'Independent website actor rate limiting');
echo "PASS $checks business checks (real WordPress and Zibll, isolated fixture tables)\n";
