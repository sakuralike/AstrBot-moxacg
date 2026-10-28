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
add_filter('enable_loading_object_cache_dropin', static fn() => false);
add_filter('pre_http_request', static fn() => new WP_Error('test_network_disabled'));
add_filter('pre_wp_mail', static fn() => false);
add_filter('zib_temp_dir', static fn() => sys_get_temp_dir() . '/az-theme-test');
$_SERVER['HTTP_HOST'] = 'localhost:18797';
$_SERVER['HTTPS'] = 'on';
$_SERVER['REQUEST_METHOD'] = 'GET';
$_SERVER['REQUEST_URI'] = '/';

if (!in_array($argv[1] ?? '', array('--run', '--checkin-worker', '--page-worker', '--code-worker', '--points-worker'), true)) {
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
if ($argv[1] === '--page-worker') {
    wp_set_current_user((int) $argv[3]);
    $token = $argv[4] ?? '';
    $_GET = $token ? array('az_bind' => $token) : array('az_bindings' => '1');
    if (($argv[5] ?? '') === 'authorize') {
        $_SERVER['REQUEST_METHOD'] = 'POST';
        $_POST['_wpnonce'] = wp_create_nonce('az_bind_' . $token);
    } elseif (($argv[5] ?? '') === 'issue_code') {
        $_SERVER['REQUEST_METHOD'] = 'POST';
        $_POST = array(
            '_wpnonce' => wp_create_nonce('az_code_' . $token),
            'issue_key' => bin2hex(random_bytes(16)), 'az_action' => 'issue_code',
        );
    }
    AstrBotZibll\binding_page();
    exit(2);
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

function spawn_points_worker($table_prefix, $target_sender, $request_label, $amount, $start)
{
    $process = proc_open(array(
        PHP_BINARY, __FILE__, '--points-worker', $table_prefix, $target_sender,
        $request_label, (string) $amount, (string) $start,
    ), array(0 => array('pipe', 'r'), 1 => array('pipe', 'w'), 2 => array('pipe', 'w')), $pipes);
    fclose($pipes[0]);
    return array($process, $pipes);
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

if ($argv[1] === '--points-worker') {
    $target = array('platform_id' => 'p', 'bot_self_id' => 'bot', 'group_id' => 'g', 'sender_id' => $argv[3]);
    $event = array('request_id' => hash('sha256', $argv[4]), 'event_time' => time());
    $start = (float) $argv[6];
    while (microtime(true) < $start) {
        usleep(1000);
    }
    $GLOBALS['wp_rest_server'] = new WP_REST_Server();
    do_action('rest_api_init');
    $response = call_api('points/add', array(
        'platform_id' => 'p', 'bot_self_id' => 'bot', 'group_id' => 'g', 'sender_id' => '001',
    ), array_merge(array('target_actor' => $target, 'amount' => (int) $argv[5]), $event));
    $data = $response->get_data();
    echo wp_json_encode(array('status' => $response->get_status(), 'data' => $data['data'] ?? null, 'code' => $data['code'] ?? null));
    exit;
}

if ($argv[1] === '--code-worker') {
    $payload = json_decode(base64_decode($argv[3]), true);
    while (microtime(true) < (float) $argv[4]) {
        usleep(1000);
    }
    if ($payload['action'] === 'admin') {
        wp_set_current_user($payload['uid']);
        $result = AstrBotZibll\admin_revoke_bindings(array(
            'az_action' => 'revoke_one', 'binding' => $payload['hash'],
            '_wpnonce' => wp_create_nonce('astrbot_zibll_bindings'),
        ));
        echo is_wp_error($result) ? $result->get_error_code() : $result[0]['state'];
    } elseif ($payload['action'] === 'revoke') {
        $result = AstrBotZibll\revoke_binding($payload['hash'], $payload['uid']);
        echo is_wp_error($result) ? $result->get_error_code() : $result->get_data()['data']['state'];
    } else {
        $result = AstrBotZibll\binding_code_action('binding/code/redeem', $payload['actor'], $payload['input']);
        echo is_wp_error($result) ? $result->get_error_code() : $result->get_data()['data']['state'];
    }
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
$points_uid = wp_insert_user(array('user_login' => 'fixture-points', 'user_pass' => wp_generate_password(), 'display_name' => 'Points Member'));
verify(is_int($uid) && is_int($uid2) && is_int($points_uid), 'Only isolated fixture users created');
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
$points_target_actor = array_merge($actor, array('sender_id' => '003'));
$checkin_target_actor = array_merge($actor, array('sender_id' => '004'));
verify(authorize_actor($points_target_actor, $points_uid)->get_status() === 200, 'Points target actor can bind a separate website user');
verify(authorize_actor($checkin_target_actor, $uid2)->get_status() === 200, 'Checkin target actor can bind the isolated checkin user');
clear_fixture_limits();
$profile = call_api('profile', $actor)->get_data()['data'];
verify(array_keys($profile) === array('uid', 'nickname', 'avatar_url', 'joined_days', 'level', 'points', 'post_count', 'comment_count'), 'Profile contains only eight requested fields');
verify($profile['uid'] === $uid && $profile['level'] === 1 && $profile['points'] === 0, 'Profile uses bound user and theme values');
verify(!metadata_exists('user', $uid, 'level'), 'Profile does not initialize level meta');
verify(call_api('profile', $actor, array('uid' => $uid2))->get_status() === 400, 'Client cannot choose another UID');
update_user_meta($uid, 'banned', 2);
verify(call_api('profile', $actor)->get_data()['code'] === 'account_restricted', 'Restricted account denied');
delete_user_meta($uid, 'banned');

clear_fixture_limits();
$points_add_event = array('request_id' => hash('sha256', 'points-add'), 'event_time' => time());
$points_add_input = array_merge(array('target_actor' => $points_target_actor, 'amount' => 50), $points_add_event);
$points_actor_data = AstrBotZibll\actor_data_from_array($actor);
$points_add_request_key = AstrBotZibll\points_request_key($points_actor_data['actor_hash'], $points_add_event['request_id']);
$points_add_order = 'astrbot:' . substr($points_add_request_key, 0, 24);
$points_record_has = static function ($uid, $order_num, $value, $points) {
    foreach ((array) zib_get_user_meta($uid, 'points_record', true) as $record) {
        if (is_array($record)
            && ($record['order_num'] ?? '') === $order_num
            && (int) ($record['value'] ?? 0) === $value
            && (int) ($record['points'] ?? -1) === $points
        ) {
            return true;
        }
    }
    return false;
};
$points_added = call_api('points/add', $actor, $points_add_input);
verify($points_added->get_status() === 200 && $points_added->get_data()['data']['state'] === 'completed'
    && $points_added->get_data()['data']['request_id'] === $points_add_event['request_id']
    && $points_added->get_data()['data']['action'] === 'add'
    && $points_added->get_data()['data']['uid'] === $points_uid
    && $points_added->get_data()['data']['amount'] === 50
    && $points_added->get_data()['data']['points_after'] === 50, 'Points add completes for the bound target');
verify(zibpay_get_user_points($points_uid) === 50
    && $points_record_has($points_uid, $points_add_order, 50, 50), 'Points add writes the Zibll points record');
$points_added_again = call_api('points/add', $actor, $points_add_input);
verify($points_added_again->get_status() === 200
    && $points_added_again->get_data()['data']['state'] === 'completed'
    && $points_added_again->get_data()['data']['request_id'] === $points_add_event['request_id']
    && $points_added_again->get_data()['data']['action'] === 'add'
    && zibpay_get_user_points($points_uid) === 50, 'Repeated points add is idempotent');
$points_conflict = call_api('points/add', $actor, array_merge($points_add_input, array('amount' => 40)));
verify($points_conflict->get_status() === 409 && $points_conflict->get_data()['code'] === 'request_conflict'
    && zibpay_get_user_points($points_uid) === 50, 'Reused points request with changed input is rejected');
$points_action_conflict = call_api('points/subtract', $actor, array_merge(
    array('target_actor' => $points_target_actor, 'amount' => 1), $points_add_event
));
verify($points_action_conflict->get_status() === 409 && $points_action_conflict->get_data()['code'] === 'request_conflict'
    && zibpay_get_user_points($points_uid) === 50, 'Reused points request with a different action is rejected');
$unbound_target = array_merge($actor, array('sender_id' => 'never-bound'));
$points_unbound = call_api('points/add', $actor, array_merge(
    array('target_actor' => $unbound_target, 'amount' => 1),
    array('request_id' => hash('sha256', 'points-unbound'), 'event_time' => time())
));
verify($points_unbound->get_status() === 404 && $points_unbound->get_data()['code'] === 'target_not_bound'
    && zibpay_get_user_points($points_uid) === 50, 'Unbound points target is rejected');
$points_subtract_event = array('request_id' => hash('sha256', 'points-subtract'), 'event_time' => time());
$points_subtract_input = array_merge(array('target_actor' => $points_target_actor, 'amount' => 20), $points_subtract_event);
$points_subtract_actor_data = AstrBotZibll\actor_data_from_array($actor);
$points_subtract_key = AstrBotZibll\points_request_key(
    $points_subtract_actor_data['actor_hash'], $points_subtract_event['request_id']
);
$points_subtract_order = 'astrbot:' . substr($points_subtract_key, 0, 24);
$points_subtracted = call_api('points/subtract', $actor, $points_subtract_input);
verify($points_subtracted->get_status() === 200
    && $points_subtracted->get_data()['data']['state'] === 'completed'
    && $points_subtracted->get_data()['data']['points_after'] === 30, 'Points subtract completes without a negative balance');
verify(zibpay_get_user_points($points_uid) === 30
    && $points_record_has($points_uid, $points_subtract_order, -20, 30), 'Points subtract writes a negative Zibll points record');
$points_insufficient = call_api('points/subtract', $actor, array_merge(
    array('target_actor' => $points_target_actor, 'amount' => 40),
    array('request_id' => hash('sha256', 'points-insufficient'), 'event_time' => time())
));
verify($points_insufficient->get_status() === 409
    && $points_insufficient->get_data()['code'] === 'insufficient_points'
    && zibpay_get_user_points($points_uid) === 30, 'Insufficient points never creates a negative balance');
$insufficient_status = call_api('points/status', $actor, array(
    'request_id' => hash('sha256', 'points-insufficient'),
))->get_data()['data'];
verify($insufficient_status['state'] === 'rejected'
    && $insufficient_status['request_id'] === hash('sha256', 'points-insufficient')
    && $insufficient_status['action'] === 'subtract', 'Insufficient points status retains the stable request identity');
$points_cross_group = call_api('points/add', array_merge($actor, array('group_id' => 'other')), array_merge(
    array('target_actor' => $points_target_actor, 'amount' => 10),
    array('request_id' => hash('sha256', 'points-cross-group'), 'event_time' => time())
));
verify($points_cross_group->get_status() === 403 && $points_cross_group->get_data()['code'] === 'forbidden'
    && zibpay_get_user_points($points_uid) === 30, 'Cross-group points target is rejected');

$start = microtime(true) + 3;
$workers = array(
    spawn_points_worker($table_prefix, '003', 'points-concurrent-same', 13, $start),
    spawn_points_worker($table_prefix, '003', 'points-concurrent-same', 13, $start),
);
$worker_results = array();
foreach ($workers as [$process, $pipes]) {
    $output = trim(stream_get_contents($pipes[1]));
    $errors = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $exit = proc_close($process);
    $result = json_decode($output, true);
    verify($exit === 0 && $errors === '' && is_array($result), 'Concurrent duplicate points worker exits cleanly');
    $worker_results[] = $result;
}
verify(count(array_filter($worker_results, static fn($result) => $result['status'] === 200
    && ($result['data']['state'] ?? '') === 'completed')) === 2, 'Concurrent duplicate points calls return one completed operation');
AstrBotZibll\fresh_user_meta($points_uid);
$duplicate_key = AstrBotZibll\points_request_key(
    AstrBotZibll\actor_data_from_array($actor)['actor_hash'], hash('sha256', 'points-concurrent-same')
);
verify(zibpay_get_user_points($points_uid) === 43
    && $points_record_has($points_uid, 'astrbot:' . substr($duplicate_key, 0, 24), 13, 43),
    'Concurrent duplicate points calls apply exactly once with a matching ledger entry');

$start = microtime(true) + 3;
$workers = array(
    spawn_points_worker($table_prefix, '003', 'points-concurrent-a', 7, $start),
    spawn_points_worker($table_prefix, '003', 'points-concurrent-b', 11, $start),
);
$worker_results = array();
foreach ($workers as [$process, $pipes]) {
    $output = trim(stream_get_contents($pipes[1]));
    $errors = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $exit = proc_close($process);
    $result = json_decode($output, true);
    verify($exit === 0 && $errors === '' && is_array($result), 'Concurrent distinct points worker exits cleanly');
    $worker_results[] = $result;
}
AstrBotZibll\fresh_user_meta($points_uid);
$completed_points_workers = count(array_filter($worker_results, static fn($result) => $result['status'] === 200
    && ($result['data']['state'] ?? '') === 'completed'));
verify($completed_points_workers === 2,
    'Concurrent distinct points operations both complete: ' . wp_json_encode($worker_results));
AstrBotZibll\fresh_user_meta($points_uid);
verify(zibpay_get_user_points($points_uid) === 61,
    'Concurrent distinct points operations preserve both balance updates');

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
verify(array_keys($items[0]) === array('title', 'published_at', 'url', 'cover_url'), 'Search returns cover URL but excludes content');
verify($items[0]['cover_url'] === '', 'Article without an image has an empty cover URL');
$latest = call_api('articles/latest', $actor)->get_data()['data']['items'];
verify(count($latest) === 2, 'Latest excludes draft/private/password posts');
verify(array_keys($latest[0]) === array('title', 'published_at', 'url'), 'Latest response remains unchanged');
verify(call_api('articles/search', $actor, array('keyword' => ''))->get_status() === 400, 'Empty keyword rejected');
$hidden = static fn($visible, $post) => $post->post_title !== 'Public needle';
add_filter('astrbot_zibll_article_visible', $hidden, 10, 2);
verify(call_api('articles/search', $actor, array('keyword' => 'needle'))->get_data()['data']['items'] === array(), 'Site visibility filter can exclude public metadata');
remove_filter('astrbot_zibll_article_visible', $hidden);

clear_fixture_limits();
$isolated_upload_dir = static function ($dirs) {
    $dirs['basedir'] = __DIR__ . '/uploads';
    $dirs['path'] = $dirs['basedir'] . $dirs['subdir'];
    return $dirs;
};
add_filter('upload_dir', $isolated_upload_dir);
$empty_category = wp_insert_term('No cover', 'category')['term_id'];
$cover_categories = array();
foreach (array('one', 'two') as $name) {
    $cover_categories[$name] = wp_insert_term('Cover category ' . $name, 'category')['term_id'];
    zib_update_term_meta($cover_categories[$name], 'cover_image', 'https://cdn.example.com/category-' . $name . '.png');
}
$attachment = wp_insert_attachment(array(
    'post_title' => 'Cover attachment', 'post_status' => 'inherit', 'post_mime_type' => 'image/jpeg',
    'guid' => home_url('/wp-content/uploads/cover-fixture.jpg'),
));
update_post_meta($attachment, '_wp_attached_file', 'cover-fixture.jpg');
update_post_meta($attachment, '_wp_attachment_metadata', array(
    'width' => 800, 'height' => 600, 'file' => 'cover-fixture.jpg',
    'sizes' => array('medium' => array(
        'file' => 'cover-fixture-300x225.jpg', 'width' => 300, 'height' => 225, 'mime-type' => 'image/jpeg',
    )),
));
$expected_covers = array(
    'Featured' => wp_get_upload_dir()['baseurl'] . '/cover-fixture-300x225.jpg',
    'External' => 'https://cdn.example.com/external.png',
    'First' => 'https://cdn.example.com/first.png',
    'Category one' => 'https://cdn.example.com/category-one.png',
    'Category two' => 'https://cdn.example.com/category-two.png',
    'Empty' => '',
    'Invalid' => '',
);
$cover_posts = array();
foreach ($expected_covers as $name => $expected) {
    $cover_posts[$name] = wp_insert_post(array(
        'post_title' => 'Cover fixture ' . $name, 'post_status' => 'publish', 'post_author' => $uid,
        'post_content' => in_array($name, array('Featured', 'External', 'First'), true)
            ? '<img src="https://cdn.example.com/first.png">' : '',
        'post_category' => array($cover_categories[substr($name, 9)] ?? $empty_category),
    ));
}
update_post_meta($cover_posts['Featured'], '_thumbnail_id', $attachment);
zib_update_post_meta($cover_posts['Featured'], 'thumbnail_url', 'https://cdn.example.com/ignored.png');
zib_update_post_meta($cover_posts['External'], 'thumbnail_url', '//cdn.example.com/external.png');
zib_update_post_meta($cover_posts['Invalid'], 'thumbnail_url', 'javascript:alert(1)');
$had_post = array_key_exists('post', $GLOBALS);
$previous_post = $GLOBALS['post'] ?? null;
$GLOBALS['post'] = get_post($cover_posts['Category one']);
$original_post = $GLOBALS['post'];
$original_user = get_current_user_id();
$items = call_api('articles/search', $actor, array('keyword' => 'Cover fixture', 'count' => 10))->get_data()['data']['items'];
verify(count($items) === count($expected_covers), 'Search includes every public cover fixture');
foreach ($items as $item) {
    $name = substr($item['title'], strlen('Cover fixture '));
    verify($item['cover_url'] === $expected_covers[$name], 'Theme cover selection: ' . $name . ' ' . $item['cover_url']);
}
verify($GLOBALS['post'] === $original_post, 'Cover lookup restores the original global post');
verify(get_current_user_id() === $original_user, 'Cover lookup restores the original current user');
unset($GLOBALS['post']);
call_api('articles/search', $actor, array('keyword' => 'Cover fixture', 'count' => 10));
verify(!array_key_exists('post', $GLOBALS), 'Cover lookup preserves an unset global post');
if ($had_post) {
    $GLOBALS['post'] = $previous_post;
}
remove_filter('upload_dir', $isolated_upload_dir);

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
$points_worker = spawn_points_worker($table_prefix, '004', 'checkin-lock-shared', 5, $start);
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
$points_output = trim(stream_get_contents($points_worker[1][1]));
$points_errors = stream_get_contents($points_worker[1][2]);
fclose($points_worker[1][1]);
fclose($points_worker[1][2]);
$points_exit = proc_close($points_worker[0]);
$points_result = json_decode($points_output, true);
verify($points_exit === 0 && $points_errors === '' && is_array($points_result)
    && $points_result['status'] === 200 && ($points_result['data']['state'] ?? '') === 'completed',
    'Points operation shares the per-user lock with concurrent checkin');
AstrBotZibll\fresh_user_meta($uid2);
verify(zibpay_get_user_points($uid2) === 15 && zib_get_user_checkin_all_day($uid2) === 1, 'Parallel calls apply checkin and admin points once each');
$ledger = zib_get_user_meta($uid2, 'points_record', true);
verify(count($ledger) === 2, 'Web AJAX checkin and bridge points transaction retain both ledger records');

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

function render_binding_fixture($uid, $token = '', $action = '')
{
    global $wpdb;
    $process = proc_open(array(
        PHP_BINARY, __FILE__, '--page-worker', $wpdb->prefix, (string) $uid, $token, $action,
    ), array(0 => array('pipe', 'r'), 1 => array('pipe', 'w'), 2 => array('pipe', 'w')), $pipes);
    fclose($pipes[0]);
    $html = stream_get_contents($pipes[1]);
    $errors = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    verify(proc_close($process) === 0 && $errors === '', 'Binding page renders without PHP errors: ' . $errors);
    return $html;
}

clear_fixture_limits();
$page_actor = array_merge($actor, array('sender_id' => str_repeat('F1799F40', 16)));
$start = call_api('binding/start', $page_actor)->get_data()['data'];
parse_str(parse_url($start['url'], PHP_URL_QUERY), $query);
$page_token = $query['az_bind'];
$page = render_binding_fixture($uid, $page_token);
verify(str_contains($page, 'class="az-shell"') && str_contains($page, 'backdrop-filter: blur('), 'Binding page includes centered glass styling');
verify(preg_match('/<style nonce="[0-9a-f]{32}">/', $page) === 1, 'Styles carry a per-response CSP nonce');
verify(str_contains($page, ':root{--az-background:#fff0f5;}'), 'Existing settings receive the default pink background');
verify(str_contains($page, 'name="_wpnonce"') && str_contains($page, 'method="post"'), 'Styled authorization form retains POST and CSRF');
verify(str_contains($page, esc_html($page_actor['sender_id'])), 'Full group identity is available for review');
verify(!str_contains($page, AstrBotZibll\settings()['secret']), 'Binding page does not expose the bridge secret');
$page_options = AstrBotZibll\settings();
$page_options['binding_background_color'] = '#dce8f2';
$page_options['binding_background_image'] = 'https://images.example.com/wallpaper.svg?size=large&crop=cover';
update_option(AstrBotZibll\OPTION, $page_options, false);
$custom_page = render_binding_fixture($uid, $page_token);
verify(str_contains($custom_page, ':root{--az-background:#dce8f2;}'), 'Custom background color reaches the rendered CSS');
verify(str_contains($custom_page, 'src="' . esc_url($page_options['binding_background_image']) . '"'), 'Custom background image URL preserves query parameters');
verify(str_contains($custom_page, 'referrerpolicy="no-referrer" hidden'), 'Background image suppresses the referrer and stays hidden until loaded');
preg_match('/<style nonce="([0-9a-f]{32})">/', $custom_page, $page_nonce);
verify(str_contains($custom_page, '<script nonce="' . $page_nonce[1] . '">'), 'Background reveal script carries the response CSP nonce');
update_option(AstrBotZibll\OPTION, array_merge($page_options, array(
    'binding_background_color' => '#ffffff;display:none',
    'binding_background_image' => 'https://images.example.com/")</style><script>alert(1)</script>',
)), false);
$escaped_page = render_binding_fixture($uid, $page_token);
verify(substr_count($escaped_page, '</style>') === 1 && !str_contains($escaped_page, '<script>'), 'Stored image values cannot inject markup');
verify(str_contains($escaped_page, ':root{--az-background:#fff0f5;}'), 'Invalid stored colors fall back safely');
$page_options['binding_background_image'] = '';
update_option(AstrBotZibll\OPTION, $page_options, false);
$color_page = render_binding_fixture($uid, $page_token);
verify(str_contains($color_page, ':root{--az-background:#dce8f2;}') && !str_contains($color_page, '<img'), 'Custom color works without an image');
$authorized_page = render_binding_fixture($uid, $page_token, 'authorize');
verify(str_contains($authorized_page, '网站授权已记录，请回原群核对昵称和 UID'), 'Styled form submission still requires original-group confirmation');
$page_status = call_api('binding/status', $page_actor)->get_data()['data'];
verify($page_status['state'] === 'awaiting_confirmation', 'Website rendering never bypasses group confirmation');
verify(call_api('binding/confirm', $page_actor, array(
    'expected_uid' => $uid, 'challenge_id' => $page_status['challenge_id'],
))->get_data()['data']['state'] === 'bound', 'Original-group confirmation completes the styled authorization flow');
$management_page = render_binding_fixture($uid);
$expired_page = render_binding_fixture($uid, str_repeat('0', 64));
verify(str_contains($expired_page, '链接已失效') && !str_contains($expired_page, 'class="az-button az-button-primary"'), 'Expired links have no authorization button');
$empty_page = render_binding_fixture($uid6);
verify(str_contains($empty_page, '还没有已绑定的群聊'), 'Empty bindings have a visible state');
verify(str_contains($management_page, 'name="binding"') && str_contains($management_page, '撤销此群授权'), 'Existing bindings retain the revocation form');
if ($preview_dir = getenv('AZ_BINDING_PREVIEW_DIR')) {
    foreach (array('default' => $page, 'custom' => $custom_page, 'color' => $color_page, 'authorized' => $authorized_page, 'expired' => $expired_page, 'empty' => $empty_page, 'management' => $management_page) as $name => $html) {
        file_put_contents($preview_dir . '/' . $name . '.html', $html);
    }
}
require __DIR__ . '/binding-codes.php';
echo "PASS $checks business checks (real WordPress and Zibll, isolated fixture tables)\n";
