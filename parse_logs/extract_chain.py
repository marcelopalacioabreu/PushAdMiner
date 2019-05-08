# High priority things:
#

# Low priority things:
# python extract_chain_new.py ~/scratch/ad_jsgraph.log "http://www.reimagemac.com/lp/medd/index.php?tracking=expXML-mac&banner=Feed1_Open1_US_0.85_MAC4_86675&adgroup=&ads_name=Movies&keyword=86675&xml_uuid=28A11302-2457-4AE9-B5C6-A702D2745504&nms=1&lpx=mac4"
# Fix above
# When you see line 235505, , then you should take that runner into account when parsing
# line 236549. Keep a structure for these frames that are opened and when there is
# a WillLoadFrame, then immediately track that back and set up a redirection chain.
# Implemented the above fix using "pending_unknown_html_node_insert_frame_ids".
# TODO: Investigate this later and see what we are missing out.


# TODO: Inspect if Service workers are causing the bug below:
# line - 2443319 in ~/scratch/service_worker_bug.log has no runner.
# For now, this is simply handled by muting the assert in get_current_runner and in process set timeout
# Should inspect this further... later on

import argparse
import pprint
from collections import defaultdict
import timeout_decorator
from datetime import datetime
from database import db_operations

from parse_utils import parse_log_entry, ignore_entry_url, peek_next_line
from utils import process_urls

REDIRECT_LIMIT = 30
LOG_PARSER_TIMEOUT = 120  # Time out in seconds

debug_url = "https://serve.popads.net/s?cid=18&iuid=1206431516&ts=1528126113&ps=2612706935&pw=281&pl=%21BVlaJy45jInpvQV7Ao%2BO2Jn0l71RpjJ2ixo87lhiuyhSeLDZavOrsHysyuICxgcmDCOvj4pFWyvCGMy6bSYFBN9Vevq2T0DSaUsCrPuvVD3%2BL4hyNdQIxMWnlMsXTNHD6yM5sGiI6NdRgjdHCosRBWX8tBhBUZs4KY7Z9HUpvZpJZNnKLBRQI3hJLuVsc3HEhZSL8UVm0MfN0vbv8eD3wYKqXVSTwb%2FARskgZJa6AfV3PsUVOKflQzrtMNsM%2BrYg0xv16rAGJK8WCRVzgu0PYA%3D%3D&s=1375,738,1,1375,738&v=nt.v&m=275,350,1,0,0,275,350,4,0,2,-1,-1,1375,738,1375,738"
class ChainExtractor(object):
    # The generated load_url in this code doesn't match the one from the redirect chain.
    # Its better to pass it explicitly to this function (after getting it from crawler logs)
    def __init__(self, chrome_log_file,log_id, load_url=None):
        self.log_file = chrome_log_file
        self.load_url = load_url
        self.all_urls = set()
        self.ordered_urls=[]
        self.frame_urls = {}
        self.notification_logs=[]
        self.log_id=log_id

        # TODO: A frame can add a child frame that can also add an eventlistener(1),
        # or navigate frame(2)
        # While the JS spawned by the original frame is running? See below:
        # (1) line 64800 of test_js_eventlistener.txt
        # (2) line 115488 of test_iframe_src.txt
        # TODO: Are we introducing any ambiguity by not considering frame id
        # along with URL in redirections?
        # Consider this in the light of inter-frame redirections that we
        # might be missing othwerwise.
        # TODO: Our tracking of redirections misses this:
        # Consider what happens when a script includes an inline script
        # using dom.write. Since the newly added script will have the
        # original base HTML as the URL, that base HTML will be taken as the chain
        # originator.
        # But, probably, the above case is not that common as its not useful.

        # of form: frame_id --> [[],[],[]...]
        # The list can be: [frame_id, "compiled_script", script_id, script_src_URL]
        #                : [frame_id, "event_listener", event_target_id, event_name]
        #                : [frame_id, "scheduled_action", "function" or "code", value]
        #                : [frame_id, "request_animation_frame", callback_id]
        self.caller_stack = {}
        # of form: frame_id --> (target_id, event_name)

        # Push state or replace state navigation is logged in two entries
        # So, the first entry is saved below until the second entry is
        # parsed.
        self.pending_push_states = {}  # Frame ID --> src_url

        self.pending_handle_event = {} # Frame ID --> target

        # Temporary work-around for tracking window opens
        self.pending_window_open_frame_id = None

        self.pending_unknown_html_node_insert_frame_ids = set()
        self.frames_so_far = set()

        # Temporary work-around for bug related to unexplained compile scripts:
        self.ignore_script_run = None

        # New: Redirection nodes could be:
        # (frame_id, "URL", the_url)
        # (frame_id, "type", event_stuff)  --> type could be event_listener or scheduled_action

        # The redirection nodes could be:
        # EventHandler: (frameID, targetID, event_name)
        # URL
        # ScheduledAction: (frameID, code) # Note: its the same for both kinds of SAs
        # Destination --> (Source, "Reason")
        self.redirections = {}
        self.load_frame_redirections = {}
        self.parent_frames = {}
        self.child_frames = defaultdict(list)
        self.collect_redirects()
        self.process_load_frame_redirects()

    def debug_get_event_listeners(self,):
        ret_stuff = []
        for dest in self.redirections:
            if dest[1] == "event_listener":
                ret_stuff.append(dest)
        return ret_stuff

    def check_any_upstream_url_link(self, url):
        for dest in self.redirections:
            # Excluding cases where src is a about:blank URL
            if dest[1] == "URL" and dest[2] == url and not (
                    self.redirections[dest][0][1] == "URL" and
                    self.redirections[dest][0][2].strip('"') != 'about:blank'):
                return self.redirections[dest]

    def get_root_frame(self, frame_id):
        while frame_id in self.parent_frames:
            if frame_id == self.parent_frames[frame_id]:
                return frame_id
                #ipdb.set_trace()
            frame_id = self.parent_frames[frame_id]
        return frame_id

    # Check if any of the relatives of frame_id are in the frame_set
    # If so, return the matching relative.
    # If not, return None
    def check_frame_relation(self, frame_id):
        root_frame_id = self.get_root_frame(frame_id)
        frame_set = set(self.caller_stack.keys())
        # Frame Tree traversal DFS
        traversal_stack = [root_frame_id]
        while len(traversal_stack) != 0:
            frame_id = traversal_stack.pop()
            if frame_id in frame_set:
                return frame_id
            if frame_id in self.child_frames:
                traversal_stack = traversal_stack + self.child_frames[frame_id]
        return None

    @timeout_decorator.timeout(LOG_PARSER_TIMEOUT)
    def process_load_frame_redirects(self):
        for dest, src in self.load_frame_redirections.items():
            #if dest[2] == debug_url:
            #    ipdb.set_trace()
            if dest not in self.redirections:
                self.redirections[dest] = src

    # Check if the frame or its parent, one of its kids or its siblings has started
    # the current JS execution.
    # If so, return the relevant caller frame ID. If not, return None
    def check_frame_id_call_stack(self, frame_id):
        if frame_id in self.caller_stack:
            return frame_id
        # Check Parent
        if (frame_id in self.parent_frames and
                self.parent_frames[frame_id] in self.caller_stack):
            return self.parent_frames[frame_id]
        # Check kids, get grand-kids
        grand_kids = []
        if frame_id in self.child_frames:
            for child_id in self.child_frames[frame_id]:
                if child_id in self.caller_stack:
                    return child_id
                if child_id in self.child_frames:
                    grand_kids = grand_kids + self.child_frames[child_id]
        # Check siblings
        if frame_id in self.parent_frames:
            parent_id = self.parent_frames[frame_id]
            for sibling_id in self.child_frames[parent_id]:
                if sibling_id in self.caller_stack:
                    return sibling_id
        # Check Grand kids
        for grand_kid_id in grand_kids:
            if grand_kid_id in self.caller_stack:
                return grand_kid_id
        return self.check_frame_relation(frame_id)


    def get_current_runner(self, frame_id, main_frame_id=None):

        if len(self.caller_stack) == 0:
            return None
        
        frame_id = self.check_frame_id_call_stack(frame_id)
        try:
            assert (frame_id or
                main_frame_id in self.caller_stack)
        except:
            #ipdb.set_trace()
            return
        if frame_id is None:
            frame_id = main_frame_id
        assert len(self.caller_stack[frame_id]) > 0
        # Note: If returning the last one gives any problems (i.e. no further redirecting parent...)
        # then, may be we can return one above and so on... until we get to the root.
        # Note: returning 0 is wrong: sometimes, there can be element in the call stack who are not
        # exactly ancestors. For example, a handle event can happen while a scheduled action is running.
        return self.format_runner(self.caller_stack[frame_id][-1])

    # Format runner for storing in redirection chain.
    def format_runner(self, runner):
        if runner[1] == "compiled_script":
            return (runner[0], "URL", runner[3])
        elif runner[1] == "request_animation_frame":
            return runner
        # If runner has 4 elements
        else:
            return (runner[0], runner[1], (runner[2], runner[3]))


    def update_redirections(self,local_frame, frame_id, dst, src, reason, timestamp):
        src = src.replace('"','').replace(' ','')
        src = src.strip('#')
        if dst in self.all_urls:
            return True
        if src not in self.all_urls:
            self.all_urls.add(src)
            self.ordered_urls.append((timestamp,src))
        self.all_urls.add(dst)
        self.ordered_urls.append((timestamp,dst))        
        dst = {'timestamp':timestamp, 'local_frame_id':local_frame, 'target_frame_id':frame_id, 'target_url': dst}
        if src in self.frame_urls:  
            if reason in self.frame_urls[src]:         
                self.frame_urls[src][reason].extend([dst])
            else:
                self.frame_urls[src].update({reason:[dst]})           
        else:
            self.frame_urls[src] = {reason:[dst]}
        return True

    # Process entries where frame or frame_root URLs are to be used based
    # on which one might be empty
    def process_frame_based_entries(self, entries, key, reason, timestamp):
        assert 'frame_url' in entries
        if entries['frame_url'].strip('"') != "about:blank":
            frame_id = entries['frame']
            try:
                self.update_redirections(frame_id,frame_id,
                    entries[key], entries['frame_url'], reason, timestamp)
            except:
                pass
                #ipdb.set_trace()
        #elif entries['local_frame_root_url'].strip('"') != "about:blank":
        else:
            frame_id = entries['local_frame_root']
            self.update_redirections(frame_id,
                frame_id, entries[key], entries['local_frame_root_url'], reason, timestamp)
        #self.update_redirections(
        #   entries['frame_url'], entries['local_frame_root_url'], 'Parent Frame')

    def process_server_redirect(self, f, timestamp):
        entries = parse_log_entry(f)
        self.update_redirections(entries['frame'],
            entries['frame'], entries['request_url'], entries['redirect_url'],
            "Server Redirect", timestamp)

    # Frame IDs are the same across URLs.
    def process_meta_refresh(self, f, timestamp):
        entries = parse_log_entry(f)
        self.process_frame_based_entries(entries, 'refresh_url', "Meta Refresh", timestamp)

    # window.location
    # happens with the same FrameID
    def process_js_navigation(self, f, timestamp):
        entries = parse_log_entry(f)
        self.update_redirections(entries['local_frame_root'],
            entries['frame'], entries['url'], entries['local_frame_root_url'],
            'JS Navigation', timestamp)
       

    def process_window_open(self, f, timestamp):
        entries = parse_log_entry(f)
        self.pending_window_open_frame_id = entries['frame']
        #self.redirections[entries['url']] = (self.get_current_runner(entries['frame']),
        #                                      'Window Open')

    def process_load_frame(self, f, timestamp):
        entries = parse_log_entry(f)
        
        # Window Open
        frame_id = entries['frame']

       
        main_frame_id = entries['main_frame']

        if self.load_url is None and entries['load_url'].startswith('http'):
            self.load_url = entries['load_url']

        dest = (frame_id, "URL", entries['load_url'])
        dest_2 = (frame_id, entries['load_url'])

        # Else, its a window open that probably happened because of the current runner
        if (self.pending_window_open_frame_id and
                self.check_frame_id_call_stack(self.pending_window_open_frame_id)):
            runner = self.get_current_runner(self.pending_window_open_frame_id)
            self.redirections[dest] = (
                                runner, 'Window Open')
            self.parent_frames[frame_id] = self.pending_window_open_frame_id
            self.child_frames[self.pending_window_open_frame_id].append(frame_id)
            self.pending_window_open_frame_id = None
            return

     

        # Else (if no runner), then its a totally unexplained window open
        # We can atlead log the load frame.
        if entries['frame_url'].strip('"') != "about:blank":
            self.load_frame_redirections[(frame_id, "URL", entries['load_url'])] = (
                                    (frame_id, "URL", entries['frame_url']), "Load Frame")           
            self.update_redirections(entries['local_frame_root'],frame_id,entries['load_url'],entries['frame_url'],'Load Frame', timestamp)
        elif entries['local_frame_root_url'].strip('"') != "about:blank":
            self.load_frame_redirections[(frame_id, "URL", entries['load_url'])] = (
                (frame_id, "URL", entries['local_frame_root_url']), "Load Frame")
            self.update_redirections(entries['local_frame_root'],frame_id,entries['load_url'],entries['local_frame_root_url'],'Load Frame', timestamp)
            
        self.process_frame_based_entries(entries, 'load_url', "Load Frame", timestamp)

    def start_script_run(self, f, timestamp):
        # TODO: Add script ID for cross checking
        entries = parse_log_entry(f)
        # Note: There might be an active JS Runner already
        # (Ex: line 2504 in test_delayed_js_nav_sa1.txt)
        frame_id = entries['frame']
        # Script run mapping to frame_url only makes sense when there
        #  is no current runner. If not, it could be due to say, a
        # scheduled action code or eval etc.
        # Its parent_farm or child frame could also be there.
        # Hence calling check_frame_id_call_stack
        related_frame_id = self.check_frame_id_call_stack(frame_id)

        if related_frame_id is None:
            if not entries['url'] and not self.ignore_script_run:
                self.ignore_script_run = entries['frame']
                return

            try:
                assert (entries['url'])
            except Exception:
                #ipdb.set_trace()
                return
            self.caller_stack[frame_id] = []
            dest = (entries['frame'], 'URL', entries['url'])
            dst_2 = (entries['frame'],entries['url'])
            # When a script is dynamically loaded by doc.write or v8setattribute,
            # then, we already set the redirection by this stage. We should make
            # sure not to rewrite it.
            if dest not in self.redirections:
                self.process_frame_based_entries(entries, "url", "Script Load",timestamp)
            related_frame_id = frame_id
        self.caller_stack[related_frame_id].append((frame_id,
            "compiled_script", entries["scriptID"], entries['url']))

    def stop_script_run(self, f, timestamp):
        entries = parse_log_entry(f)
        frame_id = entries['frame']
        if frame_id == self.ignore_script_run:
            self.ignore_script_run = None
            return
        frame_id = self.check_frame_id_call_stack(frame_id)
        try:
            assert self.caller_stack[frame_id][-1][1] == "compiled_script"
        except:
            return
        del self.caller_stack[frame_id][-1]
        if len(self.caller_stack[frame_id]) == 0:
            del self.caller_stack[frame_id]
     
    def set_child_frame(self, entries):
        frame_id = entries['frame']
        child_frame_id = entries['child_frame']
        self.parent_frames[child_frame_id] = frame_id
        self.child_frames[frame_id].append(child_frame_id)
    
    def process_notification(self, f, timestamp):
        entries = parse_log_entry(f)
        frame_url = entries['frame_url']
        url = entries['push_notification_target_url']
        entries['push_notification_target_url'] = url[url.index('http'):] if 'http' in url else ''
        notification_target_url = entries['push_notification_target_url']
        notification_img_url = entries['push_notification_image']
        notification_body = entries['push_notification_body']
        notification_title = entries['push_notification_title']   
        entries['timestamp'] = timestamp     
        entries['log_id'] = self.log_id
        dbo = db_operations.DBOperator()
        dbo.insert_notification(entries)
        self.notification_logs.append({'timestamp':timestamp,'message':'Notification from: '+frame_url})
        self.notification_logs.append({'timestamp':timestamp,'message':'Notification shown: '+' && '.join([notification_title,notification_body,notification_img_url, notification_target_url])})
    
    def process_notification_message(self, f, timestamp):
        entries = parse_log_entry(f)
        self.notification_logs.append({'timestamp':timestamp,'message': ' '.join(entries['notification_message'])})

    @timeout_decorator.timeout(LOG_PARSER_TIMEOUT)
    def collect_redirects(self,):
        if self.log_file:
            line = self.log_file.readline()
            while line:
                #if "060217.434604" in line:
                #    ipdb.set_trace()
                method= None
                timestamp = None
                if 'LOG::Forensics' in line and line.count(':')>1:
                    time = line.split(':')[2] 
                    
                    try:
                        timestamp = datetime(2019, int(time[:2]), int(time[2:4]), int(time[5:7]),int(time[7:9]), int(time[9:11]), int(time[12:]))
                    except Exception as e:
                        timestamp = None
                if "::DidReceiveMainResourceRedirect" in line:
                    method = self.process_server_redirect
                elif "::DidHandleHttpRefresh" in line:
                    method = self.process_meta_refresh
                elif "::WillNavigateFrame" in line:
                    method = self.process_js_navigation
                elif "::WindowOpen" in line:
                    #print "WindowOpen"
                    #ipdb.set_trace()
                    method = self.process_window_open
                elif "::WillLoadFrame" in line:
                    method = self.process_load_frame
                elif "::DidCompileScript" in line:
                    method = self.start_script_run
                elif "::DidRunCompiledScriptEnd" in line:
                    method = self.stop_script_run
                elif "::WillShowNotification" in line:
                    method = self.process_notification
                elif "::DebugPrints" in line:
                    method = self.process_notification_message
                if method:
                    method(self.log_file,str(timestamp))
                line = self.log_file.readline()

    def find_frame_id(self, url):
        found = []
        for key in self.redirections:
            if key[1] == "URL" and key[2] == url:
                found.append(key[0])
        return found

    def key_lookup(self, key):
        if key in self.redirections:
            return self.redirections[key]
        ## For some ads (example: RevenueHits network ads) the event listener set up and
        ## invocation code Frame IDs don't match. Hence we have this quick fix method to
        ## ignore the Frame IDs. The matching of key[2] itself is big evidence that these
        ## 2 are related anyway.
        ## QUICKFIX
        if key[1] == "event_listener":
            for existing_key in self.redirections:
                if (existing_key[1] == "event_listener" and
                    existing_key[2] == key[2]):
                    return self.redirections[existing_key]
        return None

    @timeout_decorator.timeout(LOG_PARSER_TIMEOUT)
    def get_redirect_chain(self, finald):
        #print self.redirections
        redirections = []
        redirection_set = set()
        found = self.find_frame_id(finald)
        #print "Found FrameIDs:", found
        if len(found) == 0:
            print ("Could not find frameID for the given URL", finald)
            return
        key = (found[0], "URL", finald)
        i = 0
        lookup = self.key_lookup(key)
        while lookup is not None:
            i += 1
            #print "*** ", key, self.redirections[key][1]
            if (key, lookup[1]) in redirection_set:
                print ("Loop detected; breaking...")
                break
            redirections.append((key, lookup[1]))
            redirection_set.add((key, lookup[1]))
            key = lookup[0]
            lookup = self.key_lookup(key)
            # End criteria
            if key[1] == "URL" and key[2] == self.load_url:
                break
            if i == REDIRECT_LIMIT:
                break
        redirections.append((key, "FIRST"))
        #print "*** ", key
        #ipdb.set_trace()
        return redirections

    def find_redirect_chain(self, url):
        redirect_reasons = set(['Load Frame','Server Redirect','Meta Refresh','JS Navigation'])
        redirections=[]
        if url  not in self.frame_urls:
            return redirections
        next_urls = self.frame_urls[url] 
        if not any(key in redirect_reasons for key in next_urls):
            return redirections        
        for reason in redirect_reasons:
            if reason in next_urls:
                redirect_items = next_urls[reason]
                for redirect in redirect_items:
                    if redirect['local_frame_id']== redirect['target_frame_id']:
                        target_url = redirect['target_url']
                        redirections = redirections + [[target_url] + self.find_redirect_chain(target_url)]
                        self.ordered_urls.append(redirect)
        return redirections
    

    def get_all_redirections(self):      
        redirections = []
        if '' in self.frame_urls:
            start_urls = self.frame_urls['']            
            for item in start_urls['Load Frame']:
                url = item['target_url']
                chain =self.find_redirect_chain(url)
                if len(chain)>0:
                    self.ordered_urls.append(item)
                    redirections.append({'initial_url':url,'redirection_chain':chain})
        return redirections
                        

def service_worker_requests_logs(id, log_file):
    sw_logs =[]
    if log_file:
        line = log_file.readline()
        while line:
            if 'Service Worker' in line:
                sw_item = {}
                while line:                    
                    
                    if 'Service Worker' in line:
                        time = line[line.index('@')+1:line.index(']')]
                        time = datetime.strptime(time, ' %Y-%m-%d %H:%M:%S ')
                        sw_item['timestamp']=str(time)
                        sw_item['info'] = line[:line.index('@')].strip('').replace('[','')
                    if 'Origin' in line:
                        sw_item['sw_url'] = line.split('::')[1]
                    if 'URL ::' in line:
                        #print(line)
                        sw_item['target_url'] = line.split('::')[1]
                    if '||' in line or '***' in line:                        
                        if sw_item:
                            if 'sw_url' not in sw_item:
                                sw_item['sw_url']=''
                            sw_item['log_id'] = id
                            sw_logs.append(sw_item)
                            dbo = db_operations.DBOperator()
                            dbo.insert_service_wroker_event(sw_item)
                        break
                    line = log_file.readline()
            line = log_file.readline()
    return sw_logs


def print_events(id, log_urls):
    with open('event_logs/'+id+'.log','a+') as f:
        f.write('*************************************\n')
        f.write('Events for ID ::'+id+'\n')
        f.write('*************************************\n')
        f.write('\n')
        for item in log_urls:
            f.write('['+ item['timestamp']+']\n')
            if 'info' in item:
                f.write('\tSW EVENT :: ' + item['info']+'\n')
            if 'sw_url' in item:
                f.write('\tSW URL :: ' + item['sw_url']+'\n')
            if 'target_url' in item:
                f.write('\tTarget URL :: ' + item['target_url']+'\n')
            elif 'message' in item:
                f.write('\t NOTIFICATION :: '+item['message']+'\n')

def parse_log(id, chrome_log_file, sw_log_file):
    ce = ChainExtractor(chrome_log_file, id)
    ce.ordered_urls=[]
    ce.get_all_redirections()
    logs_urls = ce.ordered_urls + ce.notification_logs
    sw_logs = service_worker_requests_logs(id, sw_log_file)
    logs_urls = logs_urls + sw_logs 
    logs_urls.sort(key=lambda r: r['timestamp'])
    print_events(id, logs_urls)
    

