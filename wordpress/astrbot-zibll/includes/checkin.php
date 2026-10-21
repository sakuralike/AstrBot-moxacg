<?php

namespace AstrBotZibll;

defined('ABSPATH') || exit;

function daily_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_daily';
}

function operation_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_operations';
}

function fresh_user_meta($uid)
{
    wp_cache_delete($uid, 'user_meta');
    wp_cache_delete('user_meta_' . $uid, 'zib_option_meta_data');
}

function checkin_result($uid, $state, $day)
{
    fresh_user_meta($uid);
    return success(array(
        'state' => $state, 'site_day' => $day,
        'points' => _pz('points_s') ? (int) zibpay_get_user_points($uid) : null,
        'continuous_days' => (int) zib_get_user_checkin_continuous_day($uid),
        'total_days' => (int) zib_get_user_checkin_all_day($uid),
    ));
}

function reward_snapshot($uid)
{
    return array(
        'points_allowed' => _pz('points_s') && zibpay_user_is_allow_add_free_points($uid),
        'integral_allowed' => _pz('user_level_s', true) && zib_user_is_allow_add_integral($uid),
        'points' => (array) zib_get_user_meta($uid, 'points_record', true),
        'integral' => (array) zib_get_user_meta($uid, 'level_integral_detail', true),
        'free_total' => (int) zibpay_get_user_today_free_points($uid),
        'integral_total' => (int) zib_get_user_today_integral($uid),
        'observed_at' => current_time('mysql'),
    );
}

function rewards_recorded($uid, $reward, $before)
{
    if (!is_array($reward)) {
        return false;
    }
    foreach (array('points', 'integral') as $kind) {
        $value = (int) ($reward[$kind] ?? 0);
        if (!$value || !$before[$kind . '_allowed']) {
            continue;
        }
        $key = $kind === 'points' ? 'points_record' : 'level_integral_detail';
        $records = (array) zib_get_user_meta($uid, $key, true);
        if ($records === $before[$kind]) {
            return false;
        }
        $found = false;
        foreach ($records as $record) {
            if (is_array($record) && (int) ($record['value'] ?? 0) === $value
                && ($kind === 'points'
                    ? (($record['type'] ?? '') === zib_get_user_points_add_options('checkin')[0]
                        && ($record['time'] ?? '') >= substr($before['observed_at'], 0, 16)
                        && ($record['time'] ?? '') <= current_time('Y-m-d H:i'))
                    : (($record['key'] ?? '') === 'checkin'
                        && ($record['time'] ?? '') >= $before['observed_at']
                        && ($record['time'] ?? '') <= current_time('mysql')))
            ) {
                $found = true;
                break;
            }
        }
        $total = $kind === 'points' ? zibpay_get_user_today_free_points($uid) : zib_get_user_today_integral($uid);
        $previous = $kind === 'points' ? $before['free_total'] : $before['integral_total'];
        if (!$found || (int) $total < $previous + $value) {
            return false;
        }
    }
    return true;
}

function checkin($uid, $actor_hash, $input, $read_only)
{
    $id = $input['request_id'] ?? '';
    $event_time = $input['event_time'] ?? null;
    if (!is_string($id) || !preg_match('/\A[0-9a-f]{64}\z/', $id) || !is_int($event_time)) {
        return failure('event_identity_required', 400);
    }
    if (!_pz('checkin_s')) {
        return failure('checkin_disabled', 403);
    }
    return with_lock('checkin:' . $uid, static function () use ($uid, $actor_hash, $id, $event_time, $read_only) {
        global $wpdb;
        fresh_user_meta($uid);
        $valid = available_user($uid);
        if (is_wp_error($valid)) {
            return $valid;
        }
        $ops = operation_table();
        $daily = daily_table();
        $key = hash('sha256', $actor_hash . ':' . $id);
        $digest = hash('sha256', $uid . ':' . $event_time);
        $row = $wpdb->get_row($wpdb->prepare("SELECT * FROM $ops WHERE request_key = %s", $key), ARRAY_A);
        if ($row) {
            if ($row['input_hash'] !== $digest) {
                return failure('request_conflict', 409);
            }
            return checkin_result($uid, $row['state'] === 'pending' ? 'uncertain' : $row['state'], $row['site_day']);
        }
        if ($read_only) {
            return checkin_result($uid, 'not_started', current_time('Y-m-d'));
        }
        if (abs(time() - $event_time) > 180) {
            return failure('event_expired', 409);
        }
        $day = current_time('Y-m-d');
        if ($wpdb->insert($ops, array(
            'request_key' => $key, 'input_hash' => $digest, 'user_id' => $uid,
            'site_day' => $day, 'state' => 'pending', 'expires_at' => time() + 7 * DAY_IN_SECONDS,
        )) !== 1) {
            return failure('unavailable', 503);
        }
        $finish = static function ($state) use ($uid, $key, $ops, $wpdb, $day) {
            if ($wpdb->update($ops, array('state' => $state), array('request_key' => $key)) === false) {
                $state = 'uncertain';
            }
            return checkin_result($uid, $state, $day);
        };
        $record = $wpdb->get_row($wpdb->prepare(
            "SELECT state FROM $daily WHERE user_id = %d AND site_day = %s", $uid, $day
        ), ARRAY_A);
        if ($record && $record['state'] !== 'completed') {
            return $finish('uncertain');
        }
        if (zib_get_user_the_checkin_details($uid)) {
            return $finish('already_checked_in');
        }
        if ($record) {
            return $finish('uncertain');
        }
        if ($wpdb->insert($daily, array('user_id' => $uid, 'site_day' => $day, 'state' => 'pending')) !== 1) {
            return $finish('uncertain');
        }
        $before_days = (int) zib_get_user_checkin_all_day($uid);
        $before_rewards = reward_snapshot($uid);
        $write_failed = false;
        $hook_finished = false;
        $completed = static function ($id) use ($uid, &$hook_finished) {
            if ((int) $id === (int) $uid) {
                $hook_finished = true;
            }
        };
        add_action('user_checkined', $completed, PHP_INT_MAX, 1);
        // Capture SQL failures without logging SQL or any user values.
        $query_watch = static function ($query) use (&$write_failed, $wpdb) {
            if ($wpdb->last_error !== '') {
                $write_failed = true;
            }
            return $query;
        };
        add_filter('query', $query_watch, PHP_INT_MAX);
        try {
            $result = zib_user_checkin($uid);
            if ($wpdb->last_error !== '') {
                $write_failed = true;
            }
            fresh_user_meta($uid);
            $detail = zib_get_user_the_checkin_details($uid);
            $state = is_array($result) && $detail && $hook_finished && !$write_failed
                && current_time('Y-m-d') === $day
                && (int) zib_get_user_checkin_all_day($uid) === $before_days + 1
                && rewards_recorded($uid, $result, $before_rewards)
                ? 'completed' : 'uncertain';
        } catch (\Throwable $error) {
            $state = 'uncertain';
        } finally {
            remove_action('user_checkined', $completed, PHP_INT_MAX);
            remove_filter('query', $query_watch, PHP_INT_MAX);
        }
        if ($wpdb->update($daily, array('state' => $state), array('user_id' => $uid, 'site_day' => $day)) === false) {
            $state = 'uncertain';
        }
        $wpdb->query($wpdb->prepare("DELETE FROM $ops WHERE expires_at < %d LIMIT 100", time()));
        $wpdb->query($wpdb->prepare("DELETE FROM $daily WHERE site_day < %s LIMIT 100", gmdate('Y-m-d', time() - 35 * DAY_IN_SECONDS)));
        return $finish($state);
    });
}

function website_checkin_guard()
{
    if (settings()['enabled'] !== true || !get_current_user_id()) {
        return;
    }
    global $wpdb;
    $uid = get_current_user_id();
    $name = 'az_' . substr(hash('sha256', DB_NAME . ':' . $wpdb->prefix . ':checkin:' . $uid), 0, 60);
    if ((string) $wpdb->get_var($wpdb->prepare('SELECT GET_LOCK(%s, 3)', $name)) !== '1') {
        wp_send_json(array('error' => 1, 'msg' => '签到处理中，请稍后查看。'));
    }
    register_shutdown_function(static function () use ($wpdb, $name) {
        $wpdb->get_var($wpdb->prepare('SELECT RELEASE_LOCK(%s)', $name));
    });
    fresh_user_meta($uid);
    $state = $wpdb->get_var($wpdb->prepare(
        'SELECT state FROM ' . daily_table() . ' WHERE user_id = %d AND site_day = %s',
        $uid, current_time('Y-m-d')
    ));
    if ($wpdb->last_error || ($state && $state !== 'completed')) {
        wp_send_json(array('error' => 1, 'msg' => '签到状态需要核实，请勿重复操作。'));
    }
    if (zib_get_user_the_checkin_details($uid) || !_pz('checkin_s') || is_wp_error(available_user($uid))) {
        return;
    }
    if ($state) {
        wp_send_json(array('error' => 1, 'msg' => '签到状态需要核实，请勿重复操作。'));
    }
    $day = current_time('Y-m-d');
    if ($wpdb->insert(daily_table(), array('user_id' => $uid, 'site_day' => $day, 'state' => 'pending')) !== 1) {
        wp_send_json(array('error' => 1, 'msg' => '签到记录暂不可用，请稍后查询。'));
    }
    $before = reward_snapshot($uid);
    $days = (int) zib_get_user_checkin_all_day($uid);
    $reward = null;
    $finished = false;
    add_action('user_checkined', static function ($id, $data) use ($uid, &$reward) {
        if ((int) $id === (int) $uid) {
            $reward = $data;
        }
    }, PHP_INT_MAX, 2);
    $finalize = static function () use ($uid, $day, $before, $days, &$reward, &$finished, $wpdb) {
        if ($finished) {
            return;
        }
        $finished = true;
        fresh_user_meta($uid);
        $valid = $reward && current_time('Y-m-d') === $day
            && zib_get_user_the_checkin_details($uid)
            && (int) zib_get_user_checkin_all_day($uid) === $days + 1
            && rewards_recorded($uid, $reward, $before);
        $wpdb->update(daily_table(), array('state' => $valid ? 'completed' : 'uncertain'), array(
            'user_id' => $uid, 'site_day' => $day,
        ));
    };
    add_action('shutdown', $finalize, -100);
    register_shutdown_function($finalize);
}
