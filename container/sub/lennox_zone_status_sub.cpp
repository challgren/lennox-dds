// Lennox M30 "LCC Zone Status" OpenDDS-Security subscriber (Phase 4, Backend A).
//
// Creates a secure DomainParticipant on domain 0, joins the homeId partition,
// subscribes to the "LCC Zone Status" topic with the recovered QoS (RELIABLE +
// TRANSIENT_LOCAL + ignore_member_names), waits for one sample, prints it as
// JSON on stdout, and exits. Driven by research/dds/dds_bringup.py, which runs
// it with CWD = research/dds/run/ so the "file:./security/..." property paths
// resolve to the staged Phase-2 bundle.
//
// Build: research/dds/sub/build.sh (needs OpenDDS built with --security; source
// $DDS_ROOT/setenv.sh first). The six DDS-Security documents and their property
// keys are from FINDINGS.md; QoS flags from research/idl/TOPICS.md.
//
// Args:
//   -DCPSConfigFile <ini>   OpenDDS discovery/transport (opendds_rtps.ini)
//   --domain <n>            DDS domain id (default 0)
//   --topic <name>          topic name (default "LCC Zone Status")
//   --partition <homeId>    PartitionQosPolicy value (REQUIRED for real data)
//   --timeout <sec>         seconds to wait for the first sample (default 60)
//   --security-dir <dir>    dir holding the 6 docs (default ./security)

#include <dds/DCPS/Service_Participant.h>
#include <dds/DCPS/Marked_Default_Qos.h>
#include <dds/DCPS/WaitSet.h>
#include <dds/DdsDcpsCoreC.h>
#include <dds/DdsDcpsCoreTypeSupportC.h>  // built-in topic (DCPSPublication) reader
#include <dds/DdsSecurityCoreC.h>

#include "lennox_m30TypeSupportImpl.h"

#include <iostream>
#include <sstream>
#include <string>
#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <csignal>
#include <thread>
#include <vector>
#include <set>

using namespace DDS;
namespace ZS = LxZoneStatusIDL;
namespace SU = LxScheduleUpdateIDL;
namespace SC = LxSchedulesIDL;
namespace PD = Lx_PeriodIDL;
namespace MA = LxManualAwayUpdateIDL;
namespace MAS = LxManualAwayStatusIDL;
namespace AA = LxAlertActiveIDL;
namespace AC = LxAlertClearedIDL;
namespace AL = LxAlertIDL;
namespace RS = LxReminderStatusIDL;
namespace WX = LxWeatherStatusIDL;
namespace SA = LxSmartAwayStatusIDL;
namespace SY = Lx_SystemStatusIDL;
namespace OE = LxOcstEventStatusIDL;
namespace ON = LxOcstEnrollmentStatusIDL;
namespace RSS = LxReminderSensorStatusIDL;

// Latest manual-away STATE per sysID, from the "LCC Manual Away Status" topic.
// Merged into every zoneStatus sample as "manualAway" so the integration's Away
// switch can reflect true state. -1 = unknown, 0 = home, 1 = away.
#include <map>
static std::map<std::string, int> g_away_status;

// Active alerts per sysID: id -> pre-rendered JSON object. Updated by the alert
// readers; merged into every zoneStatus sample as "alerts":[...].
static std::map<std::string, std::map<unsigned long, std::string>> g_alerts;

// Other status topics merged per sysID into the zoneStatus sample.
static std::map<std::string, std::map<unsigned long, std::string>> g_reminders; // id->json
static std::map<std::string, std::string> g_weather;   // json object
static std::map<std::string, std::string> g_system;    // json object
static std::map<std::string, int> g_smartaway;         // -1 unknown / 0 / 1 enabled
static std::map<std::string, std::string> g_ocst_event;   // json (demand-response event)
static std::map<std::string, std::string> g_ocst_enroll;  // json (DR enrollment)
static std::map<std::string, std::map<unsigned long, std::string>> g_reminder_sensors; // id->json

// Period validFlag bits (research/idl/lennox_m30.idl :: Lx_PeriodIDL).
static const unsigned PERIOD_VALID_SYSTEMMODE = 8;
static const unsigned PERIOD_VALID_HSP        = 16;
static const unsigned PERIOD_VALID_CSP        = 64;
static const unsigned PERIOD_VALID_SP         = 256;
static const unsigned PERIOD_VALID_FANMODE    = 16384;
static const unsigned SCHEDULE_VALID_PERIODS = 2;      // Schedule_Valid_Periods
static const unsigned SCHEDULEUPDATE_VALID_SCHEDULE = 1; // ScheduleUpdate_Valid_Schedule

// Persistent writer on the "Owner Schedule Update" topic. Created once (per run)
// and reused for every control command: one-shot --set-csp uses it, and in
// --stream mode a stdin-reader thread drives it from the bridge server's
// commands. Mirrors the app/lennoxs30api payload:
// schedules[0].schedule.periods[0].period.<field>.
struct ScheduleWriter {
  Topic_var topic;
  Publisher_var pub;
  DataWriter_var writer;
  SU::scheduleUpdateDataWriter_var sw;

  bool init(DomainParticipant_var& dp, const std::string& partition) {
    using namespace DDS;
    SU::scheduleUpdateTypeSupport_var ts = new SU::scheduleUpdateTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg scheduleUpdate failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    topic = dp->create_topic("Owner Schedule Update", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!topic) { std::cerr << "create_topic(Owner Schedule Update) failed\n"; return false; }

    PublisherQos pub_qos; dp->get_default_publisher_qos(pub_qos);
    if (!partition.empty()) { pub_qos.partition.name.length(1); pub_qos.partition.name[0] = partition.c_str(); }
    pub = dp->create_publisher(pub_qos, 0, 0);
    if (!pub) { std::cerr << "create_publisher failed\n"; return false; }

    DataWriterQos dw_qos; pub->get_default_datawriter_qos(dw_qos);
    dw_qos.reliability.kind = RELIABLE_RELIABILITY_QOS;
    dw_qos.durability.kind = VOLATILE_DURABILITY_QOS;
    dw_qos.representation.value.length(1);
    dw_qos.representation.value[0] = XCDR2_DATA_REPRESENTATION;
    writer = pub->create_datawriter(topic, dw_qos, 0, 0);
    if (!writer) { std::cerr << "create_datawriter failed\n"; return false; }
    sw = SU::scheduleUpdateDataWriter::_narrow(writer);
    return !!sw;
  }

  // Wait up to `secs` for the device's schedule reader to associate with us.
  bool wait_for_match(int secs) {
    StatusCondition_var scnd = writer->get_statuscondition();
    scnd->set_enabled_statuses(PUBLICATION_MATCHED_STATUS);
    WaitSet_var ws = new WaitSet; ws->attach_condition(scnd);
    Duration_t wait = { 1, 0 }; ConditionSeq active;
    PublicationMatchedStatus pm; pm.current_count = 0;
    for (int i = 0; i < secs; ++i) {
      writer->get_publication_matched_status(pm);
      if (pm.current_count > 0) break;
      ws->wait(active, wait);
    }
    ws->detach_condition(scnd);
    std::cerr << "[cmd] scheduleUpdate writer matched " << pm.current_count << " reader(s)\n";
    return pm.current_count > 0;
  }

  // Override one or more period setpoints for a zone's schedule. `valid` is the
  // OR of PERIOD_VALID_* bits selecting which of mode/hsp/csp/sp are meaningful.
  bool write_period(const std::string& sys_id, unsigned schedule_id, unsigned valid,
                    int systemMode, double hsp, double csp, double sp, int fanMode) {
    if (!sw) { std::cerr << "[cmd] writer not initialized\n"; return false; }
    if (!wait_for_match(15)) { std::cerr << "[cmd] no matching reader; not writing\n"; return false; }
    if (::getenv("LENNOX_DRY_RUN")) { std::cerr << "[cmd] DRY_RUN: matched OK, skipping write\n"; return true; }

    SU::scheduleUpdate su;
    su.scheduleId = schedule_id;
    su.sysID = sys_id.c_str();
    su.schedule.name = "";
    su.schedule.periods.length(1);
    PD::period& p = su.schedule.periods[0];
    p.id = 0;
    if (valid & PERIOD_VALID_SYSTEMMODE) p.systemMode = static_cast<PD::systemModeEnum>(systemMode);
    if (valid & PERIOD_VALID_HSP)        p.hsp = hsp;
    if (valid & PERIOD_VALID_CSP)        p.csp = csp;
    if (valid & PERIOD_VALID_SP)         p.sp  = sp;
    if (valid & PERIOD_VALID_FANMODE)    p.fanMode = static_cast<PD::fanmodeEnum>(fanMode);
    p.validFlag = valid;
    su.schedule.periodCount = 1;
    su.schedule.validFlag = SCHEDULE_VALID_PERIODS;
    su.validFlag = SCHEDULEUPDATE_VALID_SCHEDULE;

    const ReturnCode_t rc = sw->write(su, HANDLE_NIL);
    std::cerr << "[cmd] WROTE scheduleUpdate scheduleId=" << schedule_id
              << " valid=" << valid << " mode=" << systemMode
              << " hsp=" << hsp << " csp=" << csp << " sp=" << sp
              << " fan=" << fanMode << " rc=" << rc << "\n";
    Duration_t settle = { 3, 0 };
    writer->wait_for_acknowledgments(settle);   // let the reliable protocol deliver
    return rc == RETCODE_OK;
  }
};

// Persistent writer on the "Owner Manual Away" topic (manualAwayUpdate).
struct AwayWriter {
  Topic_var topic;
  Publisher_var pub;
  DataWriter_var writer;
  MA::manualAwayUpdateDataWriter_var aw;
  // Debug (LENNOX_DEBUG_AWAY): a read-only reader on the SAME "Owner Manual Away"
  // topic, to discover whether the device publishes its away STATE back here.
  Subscriber_var sub_r;
  DataReader_var reader_r;
  MA::manualAwayUpdateDataReader_var ar;

  bool init(DomainParticipant_var& dp, const std::string& partition) {
    using namespace DDS;
    MA::manualAwayUpdateTypeSupport_var ts = new MA::manualAwayUpdateTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg manualAwayUpdate failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    topic = dp->create_topic("Owner Manual Away", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!topic) { std::cerr << "create_topic(Owner Manual Away) failed\n"; return false; }
    PublisherQos pub_qos; dp->get_default_publisher_qos(pub_qos);
    if (!partition.empty()) { pub_qos.partition.name.length(1); pub_qos.partition.name[0] = partition.c_str(); }
    pub = dp->create_publisher(pub_qos, 0, 0);
    if (!pub) { std::cerr << "create_publisher(away) failed\n"; return false; }
    DataWriterQos dw_qos; pub->get_default_datawriter_qos(dw_qos);
    dw_qos.reliability.kind = RELIABLE_RELIABILITY_QOS;
    dw_qos.durability.kind = VOLATILE_DURABILITY_QOS;
    dw_qos.representation.value.length(1);
    dw_qos.representation.value[0] = XCDR2_DATA_REPRESENTATION;
    writer = pub->create_datawriter(topic, dw_qos, 0, 0);
    if (!writer) { std::cerr << "create_datawriter(away) failed\n"; return false; }
    aw = MA::manualAwayUpdateDataWriter::_narrow(writer);
    if (::getenv("LENNOX_DEBUG_AWAY")) init_echo_reader(dp, partition);
    return !!aw;
  }

  // Read-only listener on the away topic (debug). If the device echoes its away
  // state here, poll_echo() logs it and we learn the read-back path.
  void init_echo_reader(DomainParticipant_var& dp, const std::string& partition) {
    using namespace DDS;
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!partition.empty()) { sq.partition.name.length(1); sq.partition.name[0] = partition.c_str(); }
    sub_r = dp->create_subscriber(sq, 0, 0);
    if (!sub_r) { std::cerr << "[debug-away] create_subscriber failed\n"; return; }
    DataReaderQos dr; sub_r->get_default_datareader_qos(dr);
    dr.reliability.kind = RELIABLE_RELIABILITY_QOS;
    dr.durability.kind  = TRANSIENT_LOCAL_DURABILITY_QOS;
    dr.representation.value.length(1);
    dr.representation.value[0] = XCDR2_DATA_REPRESENTATION;
    dr.type_consistency.kind = ALLOW_TYPE_COERCION;
    dr.type_consistency.ignore_member_names = true;
    dr.type_consistency.prevent_type_widening = false;
    dr.type_consistency.force_type_validation = false;
    reader_r = sub_r->create_datareader(topic, dr, 0, 0);
    if (!reader_r) { std::cerr << "[debug-away] create_datareader failed\n"; return; }
    ar = MA::manualAwayUpdateDataReader::_narrow(reader_r);
    std::cerr << "[debug-away] echo reader up on 'Owner Manual Away'\n";
  }

  void poll_echo() {
    if (!ar) return;
    MA::manualAwayUpdateSeq d; DDS::SampleInfoSeq inf;
    if (ar->take(d, inf, DDS::LENGTH_UNLIMITED, DDS::ANY_SAMPLE_STATE,
                 DDS::ANY_VIEW_STATE, DDS::ANY_INSTANCE_STATE) != DDS::RETCODE_OK) return;
    for (CORBA::ULong i = 0; i < d.length(); ++i)
      if (inf[i].valid_data)
        std::cerr << "[debug-away] RX manualAwayUpdate sysID=" << d[i].sysID
                  << " setAway=" << (d[i].setAway ? "true" : "false")
                  << " validFlag=" << d[i].validFlag << "\n";
  }

  bool write_away(const std::string& sys_id, bool set_away) {
    if (!aw) { std::cerr << "[cmd] away writer not initialized\n"; return false; }
    // wait up to 15s for the device's away reader to match us
    StatusCondition_var scnd = writer->get_statuscondition();
    scnd->set_enabled_statuses(PUBLICATION_MATCHED_STATUS);
    WaitSet_var ws = new WaitSet; ws->attach_condition(scnd);
    Duration_t wait = { 1, 0 }; ConditionSeq active;
    PublicationMatchedStatus pm; pm.current_count = 0;
    for (int i = 0; i < 15; ++i) {
      writer->get_publication_matched_status(pm);
      if (pm.current_count > 0) break;
      ws->wait(active, wait);
    }
    ws->detach_condition(scnd);
    std::cerr << "[cmd] manualAwayUpdate writer matched " << pm.current_count << " reader(s)\n";
    if (pm.current_count == 0) { std::cerr << "[cmd] no away reader; not writing\n"; return false; }
    if (::getenv("LENNOX_DRY_RUN")) { std::cerr << "[cmd] DRY_RUN away, skipping write\n"; return true; }
    MA::manualAwayUpdate m;
    m.sysID = sys_id.c_str();
    m.setAway = set_away;
    m.validFlag = 1;  // ManualAway_Valid_SetAway
    const ReturnCode_t rc = aw->write(m, HANDLE_NIL);
    std::cerr << "[cmd] WROTE manualAwayUpdate setAway=" << (set_away ? "true" : "false")
              << " rc=" << rc << "\n";
    Duration_t settle = { 3, 0 };
    writer->wait_for_acknowledgments(settle);
    return rc == RETCODE_OK;
  }
};

// Parse ONE stdin control line and drive the writers. Grammar (from bridge_server):
//   SET <sysID> <scheduleId> [mode=<int>] [csp=<F>] [hsp=<F>] [sp=<F>] [fan=<int>]
//   AWAY <sysID> <0|1>
// Unknown lines are ignored. Fields present set their PERIOD_VALID_* bit.
static void handle_command_line(ScheduleWriter& sched, AwayWriter& away, const std::string& line) {
  std::istringstream iss(line);
  std::string verb; iss >> verb;
  if (verb == "AWAY") {
    std::string sys_id; int on = 0;
    iss >> sys_id >> on;
    if (sys_id.empty()) { std::cerr << "[cmd] AWAY missing sysID\n"; return; }
    away.write_away(sys_id, on != 0);
    return;
  }
  if (verb != "SET") { std::cerr << "[cmd] ignoring line: " << line << "\n"; return; }
  std::string sys_id; unsigned schedule_id = 0;
  iss >> sys_id >> schedule_id;
  if (sys_id.empty()) { std::cerr << "[cmd] SET missing sysID\n"; return; }
  unsigned valid = 0; int mode = 0, fan = 0; double hsp = 0, csp = 0, sp = 0;
  std::string tok;
  while (iss >> tok) {
    const std::string::size_type eq = tok.find('=');
    if (eq == std::string::npos) continue;
    const std::string k = tok.substr(0, eq), v = tok.substr(eq + 1);
    if      (k == "mode") { mode = std::atoi(v.c_str()); valid |= PERIOD_VALID_SYSTEMMODE; }
    else if (k == "hsp")  { hsp  = std::atof(v.c_str()); valid |= PERIOD_VALID_HSP; }
    else if (k == "csp")  { csp  = std::atof(v.c_str()); valid |= PERIOD_VALID_CSP; }
    else if (k == "sp")   { sp   = std::atof(v.c_str()); valid |= PERIOD_VALID_SP; }
    else if (k == "fan")  { fan  = std::atoi(v.c_str()); valid |= PERIOD_VALID_FANMODE; }
  }
  if (!valid) { std::cerr << "[cmd] SET with no recognized fields\n"; return; }
  sched.write_period(sys_id, schedule_id, valid, mode, hsp, csp, sp, fan);
}

static volatile sig_atomic_t g_running = 1;
static void on_signal(int) { g_running = 0; }

static std::string arg(int argc, char** argv, const char* key, const std::string& def) {
  for (int i = 1; i < argc - 1; ++i) if (!std::strcmp(argv[i], key)) return argv[i + 1];
  return def;
}

static bool has_flag(int argc, char** argv, const char* key) {
  for (int i = 1; i < argc; ++i) if (!std::strcmp(argv[i], key)) return true;
  return false;
}

// Set one PropertyQosPolicy entry.
static void add_prop(PropertyQosPolicy& p, const char* name, const std::string& value) {
  const CORBA::ULong n = p.value.length();
  p.value.length(n + 1);
  p.value[n].name = name;
  p.value[n].value = value.c_str();
  p.value[n].propagate = false; // security props stay local
}

static std::string json_escape(const char* s) {
  std::string o; if (!s) return o;
  for (const char* p = s; *p; ++p) {
    switch (*p) {
      case '"': o += "\\\""; break;
      case '\\': o += "\\\\"; break;
      case '\n': o += "\\n"; break;
      case '\r': o += "\\r"; break;
      case '\t': o += "\\t"; break;
      default:
        if ((unsigned char)*p < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", (unsigned char)*p); o += b; }
        else o += *p;
    }
  }
  return o;
}

// Debug (LENNOX_DEBUG_AWAY): enumerate every topic remote participants (the M30)
// publish, via the DCPSPublication built-in topic. Logs each topic|type once.
// This is how we locate the topic carrying away/system state that zoneStatus
// doesn't expose. Uses read() (not take()) so discovery data isn't consumed.
static void dump_publications(DomainParticipant_ptr dp, std::set<std::string>& seen) {
  Subscriber_var bsub = dp->get_builtin_subscriber();
  if (!bsub) return;
  DataReader_var dr = bsub->lookup_datareader("DCPSPublication");
  if (!dr) return;
  DDS::PublicationBuiltinTopicDataDataReader_var pr =
      DDS::PublicationBuiltinTopicDataDataReader::_narrow(dr);
  if (!pr) return;
  DDS::PublicationBuiltinTopicDataSeq data;
  SampleInfoSeq info;
  if (pr->read(data, info, LENGTH_UNLIMITED, ANY_SAMPLE_STATE,
               ANY_VIEW_STATE, ANY_INSTANCE_STATE) != RETCODE_OK) return;
  for (CORBA::ULong i = 0; i < data.length(); ++i) {
    if (!info[i].valid_data) continue;
    std::string key = std::string(data[i].topic_name) + "|" + std::string(data[i].type_name);
    if (seen.insert(key).second)
      std::cerr << "[debug-topics] publishes topic='" << data[i].topic_name
                << "' type='" << data[i].type_name << "'\n";
  }
}

// Reader on "LCC Manual Away Status" -> keeps g_away_status[sysID] current, so
// the away STATE (which zoneStatus doesn't carry) reaches the integration.
struct AwayStatusReader {
  Subscriber_var sub;
  DataReader_var reader;
  MAS::manualAwayStatusDataReader_var ar;

  bool init(DomainParticipant_var& dp, const std::string& partition) {
    MAS::manualAwayStatusTypeSupport_var ts = new MAS::manualAwayStatusTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg manualAwayStatus failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    Topic_var topic = dp->create_topic("LCC Manual Away Status", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!topic) { std::cerr << "create_topic(LCC Manual Away Status) failed\n"; return false; }
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!partition.empty()) { sq.partition.name.length(1); sq.partition.name[0] = partition.c_str(); }
    sub = dp->create_subscriber(sq, 0, 0);
    if (!sub) { std::cerr << "create_subscriber(away-status) failed\n"; return false; }
    DataReaderQos dr; sub->get_default_datareader_qos(dr);
    dr.reliability.kind = RELIABLE_RELIABILITY_QOS;
    dr.durability.kind  = TRANSIENT_LOCAL_DURABILITY_QOS;  // want last state on join
    dr.representation.value.length(1);
    dr.representation.value[0] = XCDR2_DATA_REPRESENTATION;
    dr.type_consistency.kind = ALLOW_TYPE_COERCION;
    dr.type_consistency.ignore_member_names = true;
    dr.type_consistency.prevent_type_widening = false;
    dr.type_consistency.force_type_validation = false;
    reader = sub->create_datareader(topic, dr, 0, 0);
    if (!reader) { std::cerr << "create_datareader(away-status) failed\n"; return false; }
    ar = MAS::manualAwayStatusDataReader::_narrow(reader);
    return !!ar;
  }

  void poll() {
    if (!ar) return;
    MAS::manualAwayStatusSeq d; SampleInfoSeq inf;
    if (ar->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE,
                 ANY_VIEW_STATE, ANY_INSTANCE_STATE) != RETCODE_OK) return;
    for (CORBA::ULong i = 0; i < d.length(); ++i)
      if (inf[i].valid_data) {
        g_away_status[std::string(d[i].sysID)] = d[i].awayStatus ? 1 : 0;
        std::cerr << "[away-status] sysID=" << d[i].sysID
                  << " awayStatus=" << (d[i].awayStatus ? "true" : "false") << "\n";
      }
  }
};

// Readers on "LCC Alert Active" (add/update) + "LCC Alert Cleared" (remove) ->
// keep g_alerts current so active faults reach the integration.
struct AlertReader {
  Subscriber_var sub;
  AA::alertActiveDataReader_var active_r;
  AC::alertClearedDataReader_var cleared_r;

  static void apply_dr_qos(DataReaderQos& dr) {
    dr.reliability.kind = RELIABLE_RELIABILITY_QOS;
    dr.durability.kind  = TRANSIENT_LOCAL_DURABILITY_QOS;  // last state on join
    dr.representation.value.length(1);
    dr.representation.value[0] = XCDR2_DATA_REPRESENTATION;
    dr.type_consistency.kind = ALLOW_TYPE_COERCION;
    dr.type_consistency.ignore_member_names = true;
    dr.type_consistency.prevent_type_widening = false;
    dr.type_consistency.force_type_validation = false;
  }

  bool init(DomainParticipant_var& dp, const std::string& partition) {
    AA::alertActiveTypeSupport_var ts1 = new AA::alertActiveTypeSupportImpl();
    AC::alertClearedTypeSupport_var ts2 = new AC::alertClearedTypeSupportImpl();
    if (ts1->register_type(dp, "") != RETCODE_OK || ts2->register_type(dp, "") != RETCODE_OK) {
      std::cerr << "reg alert types failed\n"; return false; }
    CORBA::String_var tn1 = ts1->get_type_name(), tn2 = ts2->get_type_name();
    Topic_var t1 = dp->create_topic("LCC Alert Active", tn1, TOPIC_QOS_DEFAULT, 0, 0);
    Topic_var t2 = dp->create_topic("LCC Alert Cleared", tn2, TOPIC_QOS_DEFAULT, 0, 0);
    if (!t1 || !t2) { std::cerr << "create_topic(alerts) failed\n"; return false; }
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!partition.empty()) { sq.partition.name.length(1); sq.partition.name[0] = partition.c_str(); }
    sub = dp->create_subscriber(sq, 0, 0);
    if (!sub) { std::cerr << "create_subscriber(alerts) failed\n"; return false; }
    DataReaderQos dr; sub->get_default_datareader_qos(dr); apply_dr_qos(dr);
    DataReader_var r1 = sub->create_datareader(t1, dr, 0, 0);
    DataReader_var r2 = sub->create_datareader(t2, dr, 0, 0);
    if (!r1 || !r2) { std::cerr << "create_datareader(alerts) failed\n"; return false; }
    active_r = AA::alertActiveDataReader::_narrow(r1);
    cleared_r = AC::alertClearedDataReader::_narrow(r2);
    return !!active_r && !!cleared_r;
  }

  static std::string render(unsigned long id, const AL::alert& a) {
    std::ostringstream o;
    o << "{\"id\":" << id << ",\"code\":" << a.code
      << ",\"equipmentType\":" << a.equipmentType << ",\"count\":" << a.count
      << ",\"message\":\"" << json_escape(a.userMessage) << "\""
      << ",\"since\":\"" << json_escape(a.timestampFirst) << "\""
      << ",\"last\":\"" << json_escape(a.timestampLast) << "\"}";
    return o.str();
  }

  void poll() {
    if (active_r) {
      AA::alertActiveSeq d; SampleInfoSeq inf;
      if (active_r->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE,
                         ANY_VIEW_STATE, ANY_INSTANCE_STATE) == RETCODE_OK)
        for (CORBA::ULong i = 0; i < d.length(); ++i) {
          if (!inf[i].valid_data) continue;
          std::string sys(d[i].sysID);
          if (d[i].active.isStillActive) {
            g_alerts[sys][d[i].id] = render(d[i].id, d[i].active);
            std::cerr << "[alert] active id=" << d[i].id << " code=" << d[i].active.code
                      << " sys=" << sys << " msg=" << d[i].active.userMessage << "\n";
          } else {
            g_alerts[sys].erase(d[i].id);
          }
        }
    }
    if (cleared_r) {
      AC::alertClearedSeq d; SampleInfoSeq inf;
      if (cleared_r->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE,
                          ANY_VIEW_STATE, ANY_INSTANCE_STATE) == RETCODE_OK)
        for (CORBA::ULong i = 0; i < d.length(); ++i) {
          if (!inf[i].valid_data) continue;
          g_alerts[std::string(d[i].sysID)].erase(d[i].id);
          std::cerr << "[alert] cleared id=" << d[i].id << " sys=" << d[i].sysID << "\n";
        }
    }
  }
};

// Shared reader QoS for the per-sysID status topics (RELIABLE + TRANSIENT_LOCAL
// so we get the last-published state on join; lenient type coercion for subsets).
static void status_dr_qos(DataReaderQos& dr) {
  dr.reliability.kind = RELIABLE_RELIABILITY_QOS;
  dr.durability.kind  = TRANSIENT_LOCAL_DURABILITY_QOS;
  dr.representation.value.length(1);
  dr.representation.value[0] = XCDR2_DATA_REPRESENTATION;
  dr.type_consistency.kind = ALLOW_TYPE_COERCION;
  dr.type_consistency.ignore_member_names = true;
  dr.type_consistency.prevent_type_widening = false;
  dr.type_consistency.force_type_validation = false;
}

// Reminders (LCC Reminder Status) -> g_reminders[sysID][id].
struct ReminderReader {
  Subscriber_var sub; RS::reminderDataReader_var r;
  bool init(DomainParticipant_var& dp, const std::string& part) {
    RS::reminderTypeSupport_var ts = new RS::reminderTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg reminder failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    Topic_var t = dp->create_topic("LCC Reminder Status", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!t) { std::cerr << "create_topic(reminder) failed\n"; return false; }
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!part.empty()) { sq.partition.name.length(1); sq.partition.name[0] = part.c_str(); }
    sub = dp->create_subscriber(sq, 0, 0); if (!sub) return false;
    DataReaderQos dr; sub->get_default_datareader_qos(dr); status_dr_qos(dr);
    DataReader_var dv = sub->create_datareader(t, dr, 0, 0); if (!dv) return false;
    r = RS::reminderDataReader::_narrow(dv); return !!r;
  }
  void poll() {
    if (!r) return;
    RS::reminderSeq d; SampleInfoSeq inf;
    if (r->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE, ANY_VIEW_STATE, ANY_INSTANCE_STATE) != RETCODE_OK) return;
    for (CORBA::ULong i = 0; i < d.length(); ++i) {
      if (!inf[i].valid_data) continue;
      const auto& st = d[i].status;
      std::string exp(st.reminderExpiryTime ? (const char*)st.reminderExpiryTime : "");
      // skip empty/inactive reminder slots (no life used, not expired, no expiry).
      const bool inactive = !st.reminderExpired && st.reminderRemainingPct == 0 &&
                            (exp.empty() || exp == "0");
      if (inactive) { g_reminders[std::string(d[i].sysID)].erase(d[i].id); continue; }
      std::ostringstream o;
      o << "{\"id\":" << d[i].id
        << ",\"remainingPct\":" << st.reminderRemainingPct
        << ",\"expired\":" << (st.reminderExpired ? "true" : "false")
        << ",\"expiry\":\"" << json_escape(st.reminderExpiryTime) << "\"}";
      g_reminders[std::string(d[i].sysID)][d[i].id] = o.str();
    }
  }
};

// Weather (LCC Weather Status) -> g_weather[sysID].
struct WeatherReader {
  Subscriber_var sub; WX::weatherStatusDataReader_var r;
  bool init(DomainParticipant_var& dp, const std::string& part) {
    WX::weatherStatusTypeSupport_var ts = new WX::weatherStatusTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg weatherStatus failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    Topic_var t = dp->create_topic("LCC Weather Status", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!t) { std::cerr << "create_topic(weather) failed\n"; return false; }
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!part.empty()) { sq.partition.name.length(1); sq.partition.name[0] = part.c_str(); }
    sub = dp->create_subscriber(sq, 0, 0); if (!sub) return false;
    DataReaderQos dr; sub->get_default_datareader_qos(dr); status_dr_qos(dr);
    DataReader_var dv = sub->create_datareader(t, dr, 0, 0); if (!dv) return false;
    r = WX::weatherStatusDataReader::_narrow(dv); return !!r;
  }
  void poll() {
    if (!r) return;
    WX::weatherStatusSeq d; SampleInfoSeq inf;
    if (r->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE, ANY_VIEW_STATE, ANY_INSTANCE_STATE) != RETCODE_OK) return;
    for (CORBA::ULong i = 0; i < d.length(); ++i) {
      if (!inf[i].valid_data) continue;
      std::ostringstream o;
      o << "{\"city\":\"" << json_escape(d[i].city) << "\",\"state\":\"" << json_escape(d[i].state)
        << "\",\"humidity\":" << d[i].env.humidity << ",\"windSpeed\":" << d[i].env.windSpeed
        << ",\"cloudCoverage\":" << d[i].env.cloudCoverage;
      // live conditions (weather-service value the M30 actually displays)
      if (d[i].current.length() > 0) {
        const auto& c = d[i].current[0];
        o << ",\"temperature\":" << c.temperature
          << ",\"temperatureHigh\":" << c.temperatureHigh
          << ",\"temperatureLow\":" << c.temperatureLow
          << ",\"condition\":\"" << json_escape(c.iconDescription) << "\"";
      }
      o << "}";
      g_weather[std::string(d[i].sysID)] = o.str();
    }
  }
};

// Smart Away (LCC Smart Away Status) -> g_smartaway[sysID] = config.enabled.
struct SmartAwayReader {
  Subscriber_var sub; SA::smartAwayDataReader_var r;
  bool init(DomainParticipant_var& dp, const std::string& part) {
    SA::smartAwayTypeSupport_var ts = new SA::smartAwayTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg smartAway failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    Topic_var t = dp->create_topic("LCC Smart Away Status", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!t) { std::cerr << "create_topic(smartAway) failed\n"; return false; }
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!part.empty()) { sq.partition.name.length(1); sq.partition.name[0] = part.c_str(); }
    sub = dp->create_subscriber(sq, 0, 0); if (!sub) return false;
    DataReaderQos dr; sub->get_default_datareader_qos(dr); status_dr_qos(dr);
    DataReader_var dv = sub->create_datareader(t, dr, 0, 0); if (!dv) return false;
    r = SA::smartAwayDataReader::_narrow(dv); return !!r;
  }
  void poll() {
    if (!r) return;
    SA::smartAwaySeq d; SampleInfoSeq inf;
    if (r->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE, ANY_VIEW_STATE, ANY_INSTANCE_STATE) != RETCODE_OK) return;
    for (CORBA::ULong i = 0; i < d.length(); ++i)
      if (inf[i].valid_data) g_smartaway[std::string(d[i].sysID)] = d[i].config.enabled ? 1 : 0;
  }
};

// System status (LCC System Status) -> g_system[sysID] (outdoor temp, zones).
struct SystemReader {
  Subscriber_var sub; SY::systemStatusDataReader_var r;
  bool init(DomainParticipant_var& dp, const std::string& part) {
    SY::systemStatusTypeSupport_var ts = new SY::systemStatusTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg systemStatus failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    Topic_var t = dp->create_topic("LCC System Status", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!t) { std::cerr << "create_topic(system) failed\n"; return false; }
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!part.empty()) { sq.partition.name.length(1); sq.partition.name[0] = part.c_str(); }
    sub = dp->create_subscriber(sq, 0, 0); if (!sub) return false;
    DataReaderQos dr; sub->get_default_datareader_qos(dr); status_dr_qos(dr);
    DataReader_var dv = sub->create_datareader(t, dr, 0, 0); if (!dv) return false;
    r = SY::systemStatusDataReader::_narrow(dv); return !!r;
  }
  void poll() {
    if (!r) return;
    SY::systemStatusSeq d; SampleInfoSeq inf;
    if (r->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE, ANY_VIEW_STATE, ANY_INSTANCE_STATE) != RETCODE_OK) return;
    for (CORBA::ULong i = 0; i < d.length(); ++i) {
      if (!inf[i].valid_data) continue;
      std::ostringstream o;
      o << "{\"outdoorTemperature\":" << d[i].outdoorTemperature
        << ",\"outdoorTemperatureC\":" << d[i].outdoorTemperatureC
        << ",\"numberOfZones\":" << d[i].numberOfZones
        << ",\"singleSetpointMode\":" << (d[i].singleSetpointMode ? "true" : "false")
        << ",\"wideSetpointRange\":" << (d[i].wideSetpointRange ? "true" : "false") << "}";
      g_system[std::string(d[i].sysID)] = o.str();
    }
  }
};

// OCST demand-response: event status + enrollment status.
struct OcstReader {
  Subscriber_var sub; OE::ocstEventStatusDataReader_var er; ON::ocstEnrollmentStatusDataReader_var nr;
  bool init(DomainParticipant_var& dp, const std::string& part) {
    OE::ocstEventStatusTypeSupport_var ts1 = new OE::ocstEventStatusTypeSupportImpl();
    ON::ocstEnrollmentStatusTypeSupport_var ts2 = new ON::ocstEnrollmentStatusTypeSupportImpl();
    if (ts1->register_type(dp, "") != RETCODE_OK || ts2->register_type(dp, "") != RETCODE_OK) {
      std::cerr << "reg ocst types failed\n"; return false; }
    CORBA::String_var tn1 = ts1->get_type_name(), tn2 = ts2->get_type_name();
    Topic_var t1 = dp->create_topic("LCC Ocst Event Status", tn1, TOPIC_QOS_DEFAULT, 0, 0);
    Topic_var t2 = dp->create_topic("LCC Ocst Enrollment Status", tn2, TOPIC_QOS_DEFAULT, 0, 0);
    if (!t1 || !t2) { std::cerr << "create_topic(ocst) failed\n"; return false; }
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!part.empty()) { sq.partition.name.length(1); sq.partition.name[0] = part.c_str(); }
    sub = dp->create_subscriber(sq, 0, 0); if (!sub) return false;
    DataReaderQos dr; sub->get_default_datareader_qos(dr); status_dr_qos(dr);
    DataReader_var r1 = sub->create_datareader(t1, dr, 0, 0);
    DataReader_var r2 = sub->create_datareader(t2, dr, 0, 0);
    if (!r1 || !r2) { std::cerr << "create_datareader(ocst) failed\n"; return false; }
    er = OE::ocstEventStatusDataReader::_narrow(r1);
    nr = ON::ocstEnrollmentStatusDataReader::_narrow(r2);
    return !!er && !!nr;
  }
  void poll() {
    if (er) {
      OE::ocstEventStatusSeq d; SampleInfoSeq inf;
      if (er->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE, ANY_VIEW_STATE, ANY_INSTANCE_STATE) == RETCODE_OK)
        for (CORBA::ULong i = 0; i < d.length(); ++i) {
          if (!inf[i].valid_data) continue;
          std::ostringstream o;
          o << "{\"active\":" << (d[i].showEventStatusActive ? "true" : "false")
            << ",\"pending\":" << (d[i].showEventStatusPending ? "true" : "false")
            << ",\"allowOptOut\":" << (d[i].allowUserOptOut ? "true" : "false")
            << ",\"start\":\"" << json_escape(d[i].eventStartTime) << "\""
            << ",\"end\":\"" << json_escape(d[i].eventEndTime) << "\"}";
          g_ocst_event[std::string(d[i].sysID)] = o.str();
        }
    }
    if (nr) {
      ON::ocstEnrollmentStatusSeq d; SampleInfoSeq inf;
      if (nr->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE, ANY_VIEW_STATE, ANY_INSTANCE_STATE) == RETCODE_OK)
        for (CORBA::ULong i = 0; i < d.length(); ++i) {
          if (!inf[i].valid_data) continue;
          std::ostringstream o;
          o << "{\"supported\":" << (d[i].isAHRI1380DREnrollmentSupported ? "true" : "false")
            << ",\"enrolled\":" << (d[i].isAHRI1380DRProgramSelected ? "true" : "false")
            << ",\"registrationID\":\"" << json_escape(d[i].registrationID) << "\"}";
          g_ocst_enroll[std::string(d[i].sysID)] = o.str();
        }
    }
  }
};

// Reminder sensors (LCC Reminder Sensor Status) -> g_reminder_sensors[sysID][id].
struct ReminderSensorReader {
  Subscriber_var sub; RSS::reminderSensorDataReader_var r;
  bool init(DomainParticipant_var& dp, const std::string& part) {
    RSS::reminderSensorTypeSupport_var ts = new RSS::reminderSensorTypeSupportImpl();
    if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "reg reminderSensor failed\n"; return false; }
    CORBA::String_var tn = ts->get_type_name();
    Topic_var t = dp->create_topic("LCC Reminder Sensor Status", tn, TOPIC_QOS_DEFAULT, 0, 0);
    if (!t) { std::cerr << "create_topic(reminderSensor) failed\n"; return false; }
    SubscriberQos sq; dp->get_default_subscriber_qos(sq);
    if (!part.empty()) { sq.partition.name.length(1); sq.partition.name[0] = part.c_str(); }
    sub = dp->create_subscriber(sq, 0, 0); if (!sub) return false;
    DataReaderQos dr; sub->get_default_datareader_qos(dr); status_dr_qos(dr);
    DataReader_var dv = sub->create_datareader(t, dr, 0, 0); if (!dv) return false;
    r = RSS::reminderSensorDataReader::_narrow(dv); return !!r;
  }
  void poll() {
    if (!r) return;
    RSS::reminderSensorSeq d; SampleInfoSeq inf;
    if (r->take(d, inf, LENGTH_UNLIMITED, ANY_SAMPLE_STATE, ANY_VIEW_STATE, ANY_INSTANCE_STATE) != RETCODE_OK) return;
    for (CORBA::ULong i = 0; i < d.length(); ++i) {
      if (!inf[i].valid_data) continue;
      const auto& st = d[i].status;
      std::string exp(st.reminderExpiredTime ? (const char*)st.reminderExpiredTime : "");
      const bool inactive = !st.reminderExpired && st.remainingPct == 0 && exp.empty();
      if (inactive) { g_reminder_sensors[std::string(d[i].sysID)].erase(d[i].id); continue; }
      std::ostringstream o;
      o << "{\"id\":" << d[i].id << ",\"remainingPct\":" << st.remainingPct
        << ",\"expired\":" << (st.reminderExpired ? "true" : "false")
        << ",\"replaced\":\"" << json_escape(st.replacedDate) << "\"}";
      g_reminder_sensors[std::string(d[i].sysID)][d[i].id] = o.str();
    }
  }
};

static void print_sample_json(const ZS::zoneStatus& z) {
  std::ostringstream o;
  o << "{";
  o << "\"zoneId\":" << z.zoneId;
  o << ",\"sysID\":\"" << json_escape(z.sysID.in()) << "\"";
  o << ",\"temperature\":" << z.temperature;
  o << ",\"temperatureC\":" << z.temperatureC;
  o << ",\"humidity\":" << z.humidity;
  o << ",\"tempOperation\":" << (int)z.tempOperation;
  o << ",\"humOperation\":" << (int)z.humOperation;
  o << ",\"fan\":" << (z.fan ? "true" : "false");
  // status flags (meaningful iff the matching ZoneStatus_Valid_* bit is set;
  // e.g. allergenDefender=256, ventilation=512, aux=1024, ssr=2048, defrost=4096,
  // heatCoast=8192, coolCoast=16384). Streamed for all devices; the HA integration
  // only surfaces the ones the device marks valid.
  o << ",\"allergenDefender\":" << (z.allergenDefender ? "true" : "false");
  o << ",\"ventilation\":" << (z.ventilation ? "true" : "false");
  o << ",\"aux\":" << (z.aux ? "true" : "false");
  o << ",\"ssr\":" << (z.ssr ? "true" : "false");
  o << ",\"defrost\":" << (z.defrost ? "true" : "false");
  o << ",\"heatCoast\":" << (z.heatCoast ? "true" : "false");
  o << ",\"coolCoast\":" << (z.coolCoast ? "true" : "false");
  o << ",\"tempStatus\":" << (int)z.tempStatus;
  o << ",\"humidityStatus\":" << (int)z.humidityStatus;
  o << ",\"balancePoint\":" << (int)z.balancePoint;
  o << ",\"validFlag\":" << z.validFlag;
  // active period setpoints (present iff ZoneStatus_Valid_Period bit set)
  o << ",\"period\":{";
  o << "\"systemMode\":" << (int)z.period.systemMode;
  o << ",\"fanMode\":" << (int)z.period.fanMode;
  o << ",\"sp\":" << z.period.sp << ",\"spC\":" << z.period.spC;
  o << ",\"hsp\":" << z.period.hsp << ",\"hspC\":" << z.period.hspC;
  o << ",\"csp\":" << z.period.csp << ",\"cspC\":" << z.period.cspC;
  o << ",\"husp\":" << z.period.husp << ",\"desp\":" << z.period.desp;
  o << ",\"away\":" << (z.period.away ? "true" : "false");
  o << "}";
  // schedule exceptions (field 18): manual away / holds likely show up here as an
  // active exception -> candidate for a reliable away-state indicator.
  o << ",\"scheduleExceptionIds\":[";
  for (CORBA::ULong i = 0; i < z.scheduleExceptionIds.length(); ++i) {
    if (i) o << ",";
    o << "{\"id\":" << z.scheduleExceptionIds[i].id
      << ",\"scheduleId\":" << z.scheduleExceptionIds[i].scheduleId << "}";
  }
  o << "]";
  // true manual-away STATE from the "LCC Manual Away Status" topic (per sysID).
  // null when we haven't received a status sample yet.
  {
    auto it = g_away_status.find(std::string(z.sysID));
    if (it == g_away_status.end() || it->second < 0) o << ",\"manualAway\":null";
    else o << ",\"manualAway\":" << (it->second ? "true" : "false");
  }
  // active alerts (per sysID) from the "LCC Alert Active/Cleared" topics
  {
    o << ",\"alerts\":[";
    auto it = g_alerts.find(std::string(z.sysID));
    if (it != g_alerts.end()) {
      bool first = true;
      for (const auto& kv : it->second) { if (!first) o << ","; o << kv.second; first = false; }
    }
    o << "]";
  }
  // reminders (per sysID)
  {
    o << ",\"reminders\":[";
    auto it = g_reminders.find(std::string(z.sysID));
    if (it != g_reminders.end()) {
      bool first = true;
      for (const auto& kv : it->second) { if (!first) o << ","; o << kv.second; first = false; }
    }
    o << "]";
  }
  // weather / system objects, smart-away flag (per sysID); null/absent until seen
  {
    std::string sys(z.sysID);
    auto w = g_weather.find(sys);
    o << ",\"weather\":" << (w != g_weather.end() ? w->second : std::string("null"));
    auto s = g_system.find(sys);
    o << ",\"system\":" << (s != g_system.end() ? s->second : std::string("null"));
    auto sa = g_smartaway.find(sys);
    if (sa == g_smartaway.end() || sa->second < 0) o << ",\"smartAwayEnabled\":null";
    else o << ",\"smartAwayEnabled\":" << (sa->second ? "true" : "false");
    // demand-response (OCST): event + enrollment
    auto oe = g_ocst_event.find(sys);
    o << ",\"drEvent\":" << (oe != g_ocst_event.end() ? oe->second : std::string("null"));
    auto on = g_ocst_enroll.find(sys);
    o << ",\"drEnrollment\":" << (on != g_ocst_enroll.end() ? on->second : std::string("null"));
    // sensor-based reminders + installed devices (per sysID)
    o << ",\"reminderSensors\":[";
    auto rsit = g_reminder_sensors.find(sys);
    if (rsit != g_reminder_sensors.end()) {
      bool f = true; for (const auto& kv : rsit->second) { if (!f) o << ","; o << kv.second; f = false; }
    }
    o << "]";
  }
  o << "}";
  std::cout << o.str() << std::endl;
}

int main(int argc, char** argv) {
  // Service_Participant consumes -DCPSConfigFile and other -DCPS* args.
  DomainParticipantFactory_var dpf = TheParticipantFactoryWithArgs(argc, argv);

  const int domain = std::atoi(arg(argc, argv, "--domain", "0").c_str());
  const std::string topic_name = arg(argc, argv, "--topic", "LCC Zone Status");
  const std::string partition = arg(argc, argv, "--partition", "");
  const int timeout = std::atoi(arg(argc, argv, "--timeout", "60").c_str());
  const std::string secdir = arg(argc, argv, "--security-dir", "./security");

  // --- DomainParticipantQos with the six DDS-Security documents ---
  DomainParticipantQos dp_qos;
  dpf->get_default_participant_qos(dp_qos);
  PropertyQosPolicy& props = dp_qos.property;
  const std::string f = "file:" + secdir + "/";
  add_prop(props, "dds.sec.auth.identity_ca",          f + "identity_ca.pem");
  add_prop(props, "dds.sec.auth.identity_certificate", f + "identity.pem");
  add_prop(props, "dds.sec.auth.private_key",          f + "identity.key");
  add_prop(props, "dds.sec.access.permissions_ca",     f + "permissions_ca.pem");
  add_prop(props, "dds.sec.access.governance",         f + "governance.xml.p7s");
  add_prop(props, "dds.sec.access.permissions",        f + "permissions.xml.p7s");

  DomainParticipant_var dp = dpf->create_participant(domain, dp_qos, 0, 0);
  if (!dp) { std::cerr << "create_participant failed (domain " << domain
                       << ") -- is OpenDDS built with --security and DCPSSecurity=1?\n"; return 2; }

  // --- register the zoneStatus type ---
  ZS::zoneStatusTypeSupport_var ts = new ZS::zoneStatusTypeSupportImpl();
  if (ts->register_type(dp, "") != RETCODE_OK) { std::cerr << "register_type failed\n"; return 3; }
  CORBA::String_var type_name = ts->get_type_name();

  Topic_var topic = dp->create_topic(topic_name.c_str(), type_name, TOPIC_QOS_DEFAULT, 0, 0);
  if (!topic) { std::cerr << "create_topic failed\n"; return 4; }

  // --- Subscriber with PartitionQosPolicy = homeId ---
  SubscriberQos sub_qos;
  dp->get_default_subscriber_qos(sub_qos);
  if (!partition.empty()) {
    sub_qos.partition.name.length(1);
    sub_qos.partition.name[0] = partition.c_str();
  } else {
    std::cerr << "WARN: no --partition (homeId); expect zero samples.\n";
  }
  Subscriber_var sub = dp->create_subscriber(sub_qos, 0, 0);
  if (!sub) { std::cerr << "create_subscriber failed\n"; return 5; }

  // --- DataReader QoS: RELIABLE + TRANSIENT_LOCAL + ignore_member_names (flags 147) ---
  DataReaderQos dr_qos;
  sub->get_default_datareader_qos(dr_qos);
  dr_qos.reliability.kind = RELIABLE_RELIABILITY_QOS;
  dr_qos.durability.kind  = TRANSIENT_LOCAL_DURABILITY_QOS;
  dr_qos.representation.value.length(1);
  dr_qos.representation.value[0] = XCDR2_DATA_REPRESENTATION; // XTypes; adjust if XCDR1
  // (a) maximal type-consistency leniency so a near-but-not-exact recovered IDL
  // still matches the device's type (coerce, ignore bounds + member names, allow
  // widening, don't force full validation).
  dr_qos.type_consistency.kind = ALLOW_TYPE_COERCION;
  dr_qos.type_consistency.ignore_sequence_bounds = true;
  dr_qos.type_consistency.ignore_string_bounds = true;
  dr_qos.type_consistency.ignore_member_names = true;
  dr_qos.type_consistency.prevent_type_widening = false;
  dr_qos.type_consistency.force_type_validation = false;

  DataReader_var reader = sub->create_datareader(topic, dr_qos, 0, 0);
  if (!reader) { std::cerr << "create_datareader failed\n"; return 6; }
  ZS::zoneStatusDataReader_var zr = ZS::zoneStatusDataReader::_narrow(reader);

  // --- optional control write (Phase 6): set cool setpoint via schedule override ---
  if (has_flag(argc, argv, "--set-csp")) {
    const long csp = std::atol(arg(argc, argv, "--set-csp", "0").c_str());
    const unsigned sid = (unsigned)std::atoi(arg(argc, argv, "--schedule-id", "32").c_str());
    const std::string sys = arg(argc, argv, "--sys-id", "");
    if (sys.empty()) {
      std::cerr << "[cmd] --set-csp requires --sys-id <sysID>\n";
    } else {
      std::cerr << "[cmd] setting csp=" << csp << " scheduleId=" << sid << " sys=" << sys << "\n";
      ScheduleWriter oneshot;
      if (oneshot.init(dp, partition))
        oneshot.write_period(sys, sid, PERIOD_VALID_CSP, 0, 0, (double)csp, 0, 0);
    }
  }

  const bool stream = has_flag(argc, argv, "--stream");
  const bool debug_topics = ::getenv("LENNOX_DEBUG_AWAY") != nullptr;
  std::set<std::string> seen_pubs;
  ReadCondition_var rc = reader->create_readcondition(ANY_SAMPLE_STATE, ANY_VIEW_STATE, ALIVE_INSTANCE_STATE);
  WaitSet_var ws = new WaitSet;
  ws->attach_condition(rc);

  // take all currently-available samples, print each as one NDJSON line on
  // stdout (stderr carries logs). Returns how many valid samples were printed.
  auto take_and_print = [&]() -> int {
    ZS::zoneStatusSeq data;
    SampleInfoSeq info;
    if (zr->take_w_condition(data, info, LENGTH_UNLIMITED, rc) != RETCODE_OK) return 0;
    int n = 0;
    for (CORBA::ULong i = 0; i < data.length(); ++i)
      if (info[i].valid_data) { print_sample_json(data[i]); ++n; }
    return n;
  };

  int rc_exit;
  if (stream) {
    // Long-running bridge mode: stream every sample forever until signalled.
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::cerr << "[bridge] streaming '" << topic_name << "' (partition='" << partition
              << "', domain " << domain << ")\n";

    // Persistent control writer + stdin command reader. The bridge server writes
    // one "SET ..." line per HA control request to our stdin; each drives a
    // scheduleUpdate. Kept in a detached thread so the sample loop is unblocked.
    static ScheduleWriter sched;
    static AwayWriter away;
    static AwayStatusReader away_status;
    static AlertReader alerts;
    static ReminderReader reminders;
    static WeatherReader weather;
    static SmartAwayReader smart_away;
    static SystemReader system_status;
    static OcstReader ocst;
    static ReminderSensorReader reminder_sensors;
    const bool have_writer = sched.init(dp, partition);
    away.init(dp, partition);   // best-effort; away control is optional
    if (away_status.init(dp, partition))  // true away-state readback
      std::cerr << "[bridge] away-status reader up on 'LCC Manual Away Status'\n";
    if (alerts.init(dp, partition))       // active fault alerts
      std::cerr << "[bridge] alert readers up on 'LCC Alert Active/Cleared'\n";
    if (reminders.init(dp, partition))
      std::cerr << "[bridge] reminder reader up on 'LCC Reminder Status'\n";
    if (weather.init(dp, partition))
      std::cerr << "[bridge] weather reader up on 'LCC Weather Status'\n";
    if (smart_away.init(dp, partition))
      std::cerr << "[bridge] smart-away reader up on 'LCC Smart Away Status'\n";
    if (system_status.init(dp, partition))
      std::cerr << "[bridge] system reader up on 'LCC System Status'\n";
    if (ocst.init(dp, partition))
      std::cerr << "[bridge] ocst reader up on 'LCC Ocst Event/Enrollment Status'\n";
    if (reminder_sensors.init(dp, partition))
      std::cerr << "[bridge] reminder-sensor reader up on 'LCC Reminder Sensor Status'\n";
    if (!have_writer) std::cerr << "[bridge] control writer init failed; reads-only\n";
    std::thread cmd_thread;
    if (have_writer) {
      cmd_thread = std::thread([]() {
        std::string line;
        while (std::getline(std::cin, line)) {
          if (!line.empty()) handle_command_line(sched, away, line);
        }
        std::cerr << "[bridge] stdin closed; command channel ended\n";
      });
      cmd_thread.detach();
    }

    Duration_t poll = { 1, 0 };
    while (g_running) {
      ConditionSeq active;
      ws->wait(active, poll);      // RETCODE_TIMEOUT when idle -- fine, just re-loop
      away_status.poll();          // refresh true away state before printing
      alerts.poll();               // refresh active alerts before printing
      reminders.poll();
      weather.poll();
      smart_away.poll();
      system_status.poll();
      ocst.poll();
      reminder_sensors.poll();
      take_and_print();
      away.poll_echo();            // debug (LENNOX_DEBUG_AWAY): log device away echo
      if (debug_topics) dump_publications(dp, seen_pubs);  // enumerate device topics
    }
    std::cerr << "[bridge] shutting down\n";
    rc_exit = 0;
  } else {
    // One-shot test mode: wait up to --timeout for the first sample.
    Duration_t dur = { timeout, 0 };
    ConditionSeq active;
    std::cerr << "waiting up to " << timeout << "s for a '" << topic_name
              << "' sample (partition='" << partition << "', domain " << domain << ")...\n";
    if (ws->wait(active, dur) == RETCODE_TIMEOUT) {
      std::cerr << "TIMEOUT: no sample. Check discovery/partition/security (see BRINGUP.md).\n";
      rc_exit = 7;
    } else {
      rc_exit = take_and_print() > 0 ? 0 : 9;
    }
  }

  ws->detach_condition(rc);
  reader->delete_readcondition(rc);
  dp->delete_contained_entities();
  dpf->delete_participant(dp);
  TheServiceParticipant->shutdown();
  return rc_exit;
}
